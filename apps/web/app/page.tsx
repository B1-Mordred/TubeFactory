"use client";

import { FormEvent, useCallback, useEffect, useRef, useState } from "react";

type Role = "admin" | "operator" | "editor" | "reviewer" | "viewer";
const PAGE_IDS = new Set(["dashboard", "workflows", "profiles", "archived", "research", "editorial", "media", "publishing", "operations", "audit", "providers", "configuration", "users", "identity"]);
type User = {
  id: string;
  username: string;
  display_name: string;
  role: Role;
  identity_provider: "local" | "oidc";
  totp_enabled: boolean;
  enabled: boolean;
  version: number;
  created_at: string;
};
type Session = { user: User; csrf_token: string };
type Capability = { available: boolean; planned_increment?: number; [key: string]: unknown };
type Capabilities = { increment: number; capabilities: Record<string, Capability> };
type ConfigVersion = {
  id: string;
  namespace: string;
  version: number;
  schema_version: string;
  document: Record<string, unknown>;
  document_hash: string;
  created_at: string;
  comment: string;
  active: boolean;
};
type AuditEvent = {
  id: string;
  occurred_at: string;
  action: string;
  target_type: string | null;
  target_id: string | null;
  correlation_id: string;
  context: Record<string, unknown>;
};
type Probe = { workflow_id: string; state: string; started_at?: string; completed_at?: string };
type WorkflowSummary = {
  workflow_id: string; workflow_type: string;
  execution_status: "RUNNING" | "COMPLETED" | "FAILED" | "CANCELLED" | "TERMINATED" | "TIMED_OUT" | "UNKNOWN";
  parent_workflow_id: string | null; correlation_id: string;
  channel_profile_id: string | null; channel_name: string | null;
  subject_profile_id: string | null; subject_name: string | null; created_at: string;
};
type AutomaticContinuation = { state: "started" | "completed" | "awaiting_input" | "not_applicable" | "reconciled"; action: string; workflow_id: string | null; message: string };
type BrandKit = {
  schema_version: 1; logo_text: string; tagline: string;
  primary: string; accent: string; background: string; surface: string; text: string; muted_text: string;
  heading_font: "sans" | "serif" | "rounded" | "mono"; body_font: "sans" | "serif" | "rounded" | "mono";
  corner_style: "square" | "soft" | "rounded"; motion_style: "still" | "calm" | "dynamic";
  image_treatment: "clean" | "editorial" | "documentary" | "cinematic"; visual_style: string;
};
type BrandKitTextField = Exclude<keyof BrandKit, "schema_version">;
type ChannelProfile = {
  id: string; slug: string; name: string; enabled: boolean; languages: string[]; version: number;
  identity: Record<string, unknown>; audience: Record<string, unknown>; editorial_rules: Record<string, unknown>;
  brand_kit: BrandKit; default_render_settings: Record<string, unknown>; default_publish_settings: Record<string, unknown>;
  created_at?: string; updated_at?: string;
};
type SubjectProfile = {
  id: string; channel_profile_id: string; name: string; enabled: boolean; topic: string; research_goal: string;
  excluded_angles: string[]; seed_queries: string[]; related_concepts: string[]; negative_keywords: string[]; languages: string[]; regions: string[];
  domain_policy: Record<string, unknown>; source_requirements: Record<string, unknown>; freshness_policy: Record<string, unknown>;
  format_policy: Record<string, unknown>; editorial_profile: Record<string, unknown>; risk: "low" | "medium" | "high";
  budget: Record<string, unknown>; opportunity_weights: Record<string, number>; approval_profile: Record<string, unknown>; version: number;
  schedule: { cron: string | null; timezone: string }; created_at?: string; updated_at?: string;
  [key: string]: unknown;
};
type ArchivedChannelProfile = ChannelProfile & { archived_at: string };
type ArchivedSubjectProfile = SubjectProfile & { archived_at: string };
type ChannelAutomationWorkflow = {
  key: string; name: string; version: number; enabled: boolean; language: string; summary: string;
  research_review?: "human_dossier" | "automatic_source_brief";
  prompts: Record<"script_writer" | "script_verifier" | "storyboard", string>;
  stages: { key: string; label: string; mode: "automatic" | "assisted" | "human_gate" }[];
  human_gates: string[];
};
type SubjectSchedule = { schedule_id: string; exists: boolean; paused: boolean; cron: string | null; timezone: string; action_count: number; next_action_times: string[] };
type SearchPlan = { subject_topic: string; strategies: { purpose: string; query: string; language: string; region: string | null }[]; falsification_queries: string[] };
type Opportunity = { id: string; version: number; subject_profile_id: string; title: string; summary: string; editorial_rationale: string; estimated_cost: Record<string, unknown>; policy_snapshot: { mode?: string; required_human_gates?: string[]; [key: string]: unknown }; decision: string; score: number | null; score_version: number | null; score_components: Record<string, number>; score_penalties: Record<string, number>; score_weights: Record<string, number>; score_reasoning: string[]; ai_qualification: null | { id: string; version: number; dimensions: Record<string, number>; confidence: number; abstained: boolean; rationale: string[]; uncertainty: string[]; resulting_score_version: number | null }; grouping_reason: string[]; source_count: number; snapshot_count: number; research_state: string | null; created_at: string };
type ArchivedOpportunity = Opportunity & { archived_at: string };
type ExplanationReadiness = { ready?: boolean; target_duration_seconds?: number; minimum_duration_seconds?: number; maximum_duration_seconds?: number; target_word_range?: number[]; minimum_factual_claims?: number; usable_claim_count?: number; minimum_coverage_units?: number; coverage_units?: Array<{ id?: string; label?: string; question?: string; role?: string }>; gaps?: string[]; enrichment_round?: number; independent_source_count?: number; primary_source_count?: number; counterevidence_search_completed?: boolean };
type Dossier = { id: string; opportunity_id: string; dossier_version: number; version: number; status: string; executive_summary: string; completion_evaluation: { complete?: boolean; blockers?: string[]; explanation_readiness?: ExplanationReadiness; [key: string]: unknown }; created_at: string; automatic_continuation?: AutomaticContinuation | null };
type DossierDetail = Dossier & { explanation_plan: Array<{ id?: string; label?: string; role?: string; question?: string }>; safe_conclusions: string[]; prohibited_overstatements: string[]; unresolved_questions: string[]; ai_evidence_assessment: null | { id: string; version: number; claim_assessments: Record<string, unknown>[]; source_assessments: Record<string, unknown>[]; methodological_limits: string[]; counterevidence_gaps: string[]; confidence: number; abstained: boolean; uncertainty: string[] }; claims: { id: string; normalized_statement: string; claim_type: string; coverage_unit_ids: string[]; confidence: number; status: string; risk: string; central: boolean; version: number; evidence: { relationship: string; exact_text: string; source_independent: boolean; direct_evidence: boolean; primary_source: boolean; source: { title: string; canonical_url: string; publisher: string | null }; snapshot: { content_hash: string; retrieved_at: string } }[] }[] };
type SourceBrowserItem = { id: string; canonical_url: string; title: string; author: string | null; publisher: string | null; source_type: string; publication_at: string | null; event_at: string | null; domain: string; snapshots: { id: string; snapshot_number: number; content_hash: string; mime_type: string; byte_size: number; retrieved_at: string; extraction_metadata: Record<string, unknown>; injection_markers: string[]; semantic_chunk_count: number }[]; relationships: { id: string; source_document_id: string; related_source_document_id: string; relationship: string; reason: string; confidence: number }[] };
type SourcePreview = { source_document_id: string; source_snapshot_id: string; content_hash: string; text: string; truncated: boolean; untrusted: boolean };
type Provider = { id: string; version: number; slug: string; name: string; driver_type: string; endpoint: string | null; enabled: boolean; location: string; authentication_scheme: string; has_secret: boolean; data_policy: string; capabilities: Record<string, unknown>; concurrency_limit: number; requests_per_minute: number; health_status: Record<string, unknown> };
type ProviderModelDiscovery = { provider_id: string; provider_name: string; driver_type: string; models: string[]; model_count: number };
type AIModel = { id: string; version: number; provider_id: string; model_name: string; display_name: string; visible: boolean; enabled: boolean; model_version: string; capabilities: { tasks?: string[]; [key: string]: unknown }; context_limit: number; output_limit: number; cost_policy: Record<string, unknown>; data_policy_override: string | null };
type AIUsage = { id: string; workflow_id: string; activity_id: string; task_type: string; provider_id: string; provider_name: string; model_id: string; model_name: string; prompt_template_id: string; request_hash: string; response_hash: string; input_tokens: number; output_tokens: number; latency_ms: number; cost: Record<string, unknown>; redaction_summary: { count?: number; removed?: Array<{ path: string; category: string; value_hash: string }> }; correlation_id: string; created_at: string };
type TaskAssignment = { id: string; task_type: string; assignment_version: number; primary_model_id: string; fallback_model_ids: string[]; routing_policy: Record<string, unknown>; budget_policy: Record<string, unknown>; comment: string; active: boolean };
type PromptTemplate = { id: string; template_key: string; task_type: string; template_version: number; system_instructions: string; template: string; input_schema: Record<string, unknown>; response_schema: Record<string, unknown>; content_hash: string; comment: string; active: boolean };
type EditorialRun = { workflow_id: string; state: string; progress: number; result: Record<string, unknown> | null; execution_status: "RUNNING" | "COMPLETED" | "FAILED" | "CANCELLED" | "TERMINATED" | "TIMED_OUT"; retryable: boolean; correlation_id: string | null };
type DirectScriptedVideoPreview = { title: string; scene_count: number; total_duration_seconds: number; word_count: number; target_wpm_min: number; target_wpm_max: number; placeholder_tokens: string[]; scenes: Array<{ scene_key: string; order: number; title: string; duration_seconds: number; word_count: number; placeholder_tokens: string[]; on_screen_text: string[]; assets: string[]; approval_note: string }> };
type ScriptSummary = { id: string; dossier_id: string | null; opportunity_id?: string | null; production_brief_id?: string | null; channel_profile_id?: string | null; source_kind: string; evidence_required: boolean; status: string; version: number; current_version_id: string; title: string; content_hash: string; coverage_percent: number; created_at: string };
type ScriptSegment = { id: string; segment_key: string; segment_order: number; segment_type: string; narration: string; presentation_purpose: string; duration_seconds: number; citation_display: Record<string, unknown>; annotations: Array<{ text: string; start_offset: number; end_offset: number; kind: string; claim_ids: string[]; evidence_excerpt_id: string | null }>; locked: boolean; content_hash: string; claim_links: Array<{ claim_id: string; evidence_excerpt_id: string | null; statement_text: string; start_offset: number; end_offset: number; statement_kind: string }> };
type ScriptDetail = ScriptSummary & { verification_report: { valid?: boolean; deterministic_valid?: boolean; requires_independent_verification?: boolean; evidence_required?: boolean; mode?: string; placeholder_tokens?: string[]; unresolved_placeholders?: string[]; issues?: Array<{ code: string; message: string; segment_key: string | null; statement: string | null }> }; segments: ScriptSegment[]; automatic_continuation?: AutomaticContinuation | null };
type StoryboardSummary = { id: string; script_id: string; status: string; version: number; current_version_id: string; content_hash: string; scene_count: number; created_at: string };
type StoryboardDetail = StoryboardSummary & { script_version_id: string; scenes: Array<{ id: string; scene_key: string; locked: boolean; aggregate_version: number; version: number; content_hash: string; scene_spec: Record<string, unknown> }>; automatic_continuation?: AutomaticContinuation | null };
type SceneAlternative = { id: string; scene_id: string; base_scene_version_id: string; alternative_number: number; scene_spec: Record<string, unknown>; content_hash: string; model_id: string; prompt_template_id: string; instruction: string; workflow_id: string; created_at: string; current_base: boolean };
type ComfyWorkflow = { id: string; workflow_key: string; version_number: number; purpose: string; content_hash: string; approval_state: string; active: boolean; required_nodes: Array<Record<string, unknown>>; required_models: Array<Record<string, unknown>> };
type VoiceProfile = { id: string; profile_key: string; version_number: number; provider_type: string; language: string; engine: string; model_version: string; content_hash: string; enabled: boolean; active: boolean };
type MediaAsset = { id: string; scene_version_id: string | null; asset_kind: string; object_key: string; content_hash: string; mime_type: string; byte_size: number; width: number | null; height: number | null; duration_seconds: number | null; licence: Record<string, unknown>; generation_provenance: Record<string, unknown> };
type QAFinding = { id: string; code: string; verdict: "pass" | "warn" | "fail"; message: string; scene_version_id: string | null; timecode_seconds: number | null; details: Record<string, unknown>; override_policy: "never" | "reasoned"; overridden: boolean; override_reason: string | null };
type ManifestScene = { scene_version_id: string; scene_hash: string; scene_spec: { order?: number; purpose?: string; duration?: number; claim_ids?: string[]; source_ids?: string[]; on_screen_text?: string[]; accessibility_notes?: string } };
type ManifestSource = { id: string; title: string; url: string; publisher: string | null; author: string | null; snapshot_hash: string; retrieved_at: string };
type MediaProduction = { id: string; storyboard_version_id: string; storyboard_hash: string; workflow_id: string; render_tier: string; state: string; settings: Record<string, unknown>; correlation_id: string; created_at: string; completed_at: string | null; assets: MediaAsset[]; render: { id: string; number: number; tier: string; video_asset_id: string; content_hash: string; engine: string; engine_version: string; composition: string; settings: Record<string, unknown>; probe: Record<string, unknown>; created_at: string } | null; manifest: { id: string; version: number; content_hash: string; object_key: string; document: { scenes?: ManifestScene[]; sources?: ManifestSource[]; chapters?: Array<{ title?: string; start_seconds?: number; timecode_seconds?: number }>; captions?: MediaAsset[]; narration?: Array<{ script_segment_id: string; segment_order: number; duration_seconds: number; request: { text?: string }; response: Record<string, unknown> }>; [key: string]: unknown } } | null; qa: { id: string; verdict: string; content_hash: string; metrics: Record<string, unknown>; policy_snapshot: Record<string, unknown> } | null; findings: QAFinding[]; approval: { id: string; decision: string; comment: string; actor_id: string; created_at: string } | null; automatic_continuation?: AutomaticContinuation | null };
type PublishingConfig = { id: string | null; version_number: number; real_uploads_enabled: boolean; document: Record<string, unknown>; content_hash: string | null };
type YouTubeConnection = { id: string; channel_profile_id: string; youtube_channel_id: string; youtube_channel_title: string; granted_scopes: string[]; status: string; enabled: boolean; version: number; created_at: string; updated_at: string };
type PublishMetadata = { id: string; render_id: string; version_number: number; document: Record<string, unknown>; content_hash: string; created_by: string; created_at: string; comment: string };
type Publication = { id: string; workflow_id: string; connection_id: string; render_id: string; render_hash: string; metadata_version_id: string; metadata_hash: string; mode: "dry_run" | "real"; state: string; youtube_video_id: string | null; uploaded_bytes: number; processing_status: Record<string, unknown>; caption_status: Record<string, unknown>; thumbnail_status: Record<string, unknown>; failure: Record<string, unknown> | null; correlation_id: string; version: number; created_at: string; updated_at: string };
type AnalyticsDashboard = { snapshot_count: number; video_count: number; latest_period_end: string | null; totals: Record<string, number>; provenance: Array<{ id: string; content_hash: string; source: string; created_at: string }> };
type ModelRecommendation = { id: string; benchmark_run_id: string; task_type: string; recommended_model_id: string; recommended_fallback_model_ids: string[]; baseline_assignment_id: string | null; score: number; reasoning: string[]; recommendation_hash: string; decision: "pending" | "approved" | "rejected"; decision_id: string | null; applied: boolean; new_assignment_id: string | null; created_at: string };
type OperationalEvidence = { id: string; evidence_kind: string; document: Record<string, unknown>; content_hash: string; artifact_hash: string | null; created_at: string };

const roadmap = [
  ["Discovery", "Profiles, opportunities and source acquisition", 1],
  ["Research", "Claims, evidence and dossier review", 1],
  ["Editorial", "Cited scripts, verification and scene planning", 2],
  ["Media", "Narration, rendering and blocking quality gates", 3],
  ["Publishing", "Private upload and separate public release approval", 4],
  ["Optimization", "Analytics, benchmarks and reviewed model assignments", 5],
] as const;

const productionPath = [
  { number: 1, label: "Configure", detail: "Channel, subject, schedule", page: "profiles" },
  { number: 2, label: "Discover", detail: "Shortlist and source brief", page: "research" },
  { number: 3, label: "Script", detail: "Write and approve", page: "editorial" },
  { number: 4, label: "Storyboard", detail: "Scenes and approval", page: "editorial" },
  { number: 5, label: "Render", detail: "Media QA", page: "media" },
  { number: 6, label: "Publish", detail: "Private upload, release", page: "publishing" },
] as const;

async function api<T>(path: string, options: RequestInit = {}, csrf = ""): Promise<T> {
  const headers = new Headers(options.headers);
  if (options.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  if (csrf) headers.set("X-CSRF-Token", csrf);
  const response = await fetch(path, { ...options, headers, credentials: "same-origin" });
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail));
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

function UsageTip({ text, onClick }: { text: string; onClick: () => void }) {
  return <button type="button" className="usage-tip help-launcher" data-usage={text} aria-label={`Open task help. ${text}`} aria-haspopup="dialog" aria-controls="task-help-panel" onClick={onClick}>Help</button>;
}

function ProductionPath({ currentPage, onOpenPage }: { currentPage: string; onOpenPage: (page: string) => void }) {
  const activeStep = productionPath.find(step => step.page === currentPage)?.number;
  return <nav className="production-path" aria-label="Production path">
    {productionPath.map(step => {
      const active = activeStep === step.number;
      return <button key={`${step.number}-${step.label}`} type="button" className={active ? "active" : ""} aria-current={active ? "step" : undefined} onClick={() => onOpenPage(step.page)}>
        <span>{step.number}</span>
        <strong>{step.label}</strong>
        <small>{step.detail}</small>
      </button>;
    })}
  </nav>;
}

const fieldUsageHints: Array<[string, string]> = [
  ["channel workspace", "Choose one channel to scope channel-aware queues, or choose All channels for the portfolio view."],
  ["username", "Enter your local operator username."],
  ["password", "Enter the account password; local passwords must contain at least eight characters."],
  ["show password", "Toggle whether the password is visible on this device."],
  ["authenticator or recovery code", "Enter a current authenticator code or one unused recovery code when multi-factor authentication is enabled."],
  ["enabled subject", "Choose the enabled subject that owns this work in the current channel."],
  ["opportunity decision reason", "Record why you are shortlisting, deferring or rejecting; the reason is retained in audit history."],
  ["why does this video deserve to exist", "Explain the audience need and evidence gap this video will address."],
  ["estimated model tokens", "Estimate model usage so budget policy can prevent unexpected spending."],
  ["find workflow", "Filter workflow history by type, subject, channel or workflow ID."],
  ["schedule time", "Choose the intended publication time in your browser's local timezone."],
  ["artifact sha-256", "Enter the artifact's 64-character SHA-256 digest when one is available."],
  ["evidence json", "Describe the operational result as valid JSON; this record becomes immutable."],
  ["current password", "Confirm your current password before changing a security-sensitive setting."],
  ["new password", "Choose a new password containing at least eight characters."],
];

function normalizedUsageText(value: string): string {
  return value.replace(/\s+/g, " ").replace(/\s*\([^)]*\)\s*$/g, "").trim();
}

function usageForField(label: string, control: HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement): string {
  const key = label.toLowerCase().replace(/[?:]+$/g, "").trim();
  const curated = fieldUsageHints.find(([prefix]) => key.startsWith(prefix));
  if (curated) return curated[1];
  const subject = key || control.getAttribute("placeholder")?.toLowerCase() || "this value";
  if (control instanceof HTMLSelectElement) return `Choose ${subject}; available options reflect the current workspace and record state.`;
  if (control instanceof HTMLTextAreaElement) return `Describe ${subject}; keep the explanation specific enough for later review.`;
  if (control.type === "checkbox") return `Toggle ${subject}; the change is applied only when you submit or activate this form.`;
  if (control.type === "number") return `Enter ${subject} as a number; configured limits are enforced before submission.`;
  if (control.type === "url") return `Enter a complete URL for ${subject}, including https://.`;
  if (control.type === "datetime-local" || control.type === "date") return `Choose ${subject} using your browser's local date and time controls.`;
  if (control.type === "search") return `Type to filter by ${subject}; the underlying records are not changed.`;
  if (control.type === "password") return `Enter ${subject}; the value remains masked unless you explicitly reveal it.`;
  return `Enter ${subject}; review the value before continuing.`;
}

function usageForAction(label: string): string {
  const action = label.toLowerCase();
  if (action === "sign out") return "End your current TubeFactory session on this device.";
  if (/^refresh|^reconcile/.test(action)) return "Reload the latest recorded state without creating a duplicate workflow.";
  if (/^approve/.test(action)) return "Approve the exact version shown and automatically start its permitted next stage; verify the evidence, hashes and downstream settings first.";
  if (/^reject|^defer/.test(action)) return "Record this decision with the reason currently entered on the page.";
  if (/^shortlist/.test(action)) return "Move this opportunity forward using the recorded editorial reason and policy checks.";
  if (/^run|^start|^generate|^regenerate|^retry|^queue|^ingest/.test(action)) return "Start a durable background operation; progress and failures remain visible in Workflow activity.";
  if (/^create|^save|^update|^activate|^enable|^connect|^register|^store|^set/.test(action)) return "Apply the values shown to a versioned or audited record; review the form first.";
  if (/^disable|^remove|^delete|^force/.test(action)) return "Change availability or policy state; verify the selected target before continuing.";
  if (/^show|^open|^view/.test(action)) return "Open or reveal the related detail without changing the underlying record.";
  return `Use “${label}” to continue this step.`;
}

function useContextualUsageHints(renderKey: string) {
  useEffect(() => {
    const root = document.body;
    let frame = 0;
    const annotate = () => {
      root.querySelectorAll<HTMLLabelElement>("label").forEach(label => {
        const linked = label.htmlFor ? document.getElementById(label.htmlFor) : null;
        const control = (linked || label.querySelector("input, select, textarea")) as HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement | null;
        if (!control || label.dataset.usage) return;
        const copy = label.cloneNode(true) as HTMLLabelElement;
        copy.querySelectorAll("input, select, textarea, small").forEach(node => node.remove());
        const name = normalizedUsageText(control.getAttribute("aria-label") || copy.textContent || control.getAttribute("placeholder") || "");
        const hint = usageForField(name, control);
        label.classList.add("field-help");
        label.dataset.usage = hint;
        control.setAttribute("aria-description", hint);
      });
      root.querySelectorAll<HTMLElement>("button:not(.usage-tip):not(.nav-help), a[href]").forEach(control => {
        if (control.dataset.usage) return;
        const label = normalizedUsageText(control.textContent || control.getAttribute("aria-label") || "");
        if (!label || label.length > 80) return;
        const hint = usageForAction(label);
        control.classList.add("control-help");
        control.dataset.usage = hint;
        control.setAttribute("aria-description", hint);
      });
    };
    const schedule = () => { window.cancelAnimationFrame(frame); frame = window.requestAnimationFrame(annotate); };
    annotate();
    const observer = new MutationObserver(schedule);
    observer.observe(root, { childList: true, subtree: true });
    return () => { observer.disconnect(); window.cancelAnimationFrame(frame); };
  }, [renderKey]);
}

type HelpTask = {
  id: string;
  title: string;
  summary: string;
  category: string;
  page: string;
  roles?: Role[];
  keywords: string[];
  steps: string[];
};

const helpTasks: HelpTask[] = [
  {
    id: "create-channel-subject",
    title: "Create, edit or archive channels and subjects",
    summary: "Manage channel identity, research subjects, schedules and recoverable archival.",
    category: "Channels & subjects",
    page: "profiles",
    keywords: ["setup", "onboarding", "channel", "subject", "schedule", "cron", "audience", "brand", "edit", "delete", "archive"],
    steps: [
      "Open Channels & subjects and create the channel profile with its language, audience and editorial rules.",
      "Select the new channel from Channel workspace so later work is scoped correctly.",
      "Create a subject with a specific topic, research goal and seed queries.",
      "Review the generated search plan, including falsification queries and regional settings.",
      "Enable the subject, then add or resume its schedule only after the search plan is correct.",
      "Use Edit on an existing card to change its complete versioned profile; advanced policies are preserved unless you change them.",
      "Use Archive to remove a profile from active lists while retaining workflows and audit history. Archive or reassign a channel's subjects first.",
    ],
  },
  {
    id: "restore-profile",
    title: "Inspect the archive or restore a profile",
    summary: "Search archived Channels, subjects and scored findings; profiles can be restored safely as disabled records.",
    category: "Archive",
    page: "archived",
    keywords: ["restore", "recover", "undo", "archive", "deleted", "channel", "subject"],
    steps: [
      "Open Archive and search by profile name, finding title, score, slug, topic or parent Channel.",
      "Expand a result to inspect its archived version, configuration summary and archive time.",
      "Scored findings remain read-only in the archive and never appear on the active Opportunity Board.",
      "Restore an archived Channel before restoring any subjects that belong to it.",
      "Restore the subject. It returns disabled and its schedule stays paused until you deliberately enable and apply it.",
    ],
  },
  {
    id: "faktischsimpel-explainer",
    title: "Produce a FaktischSimpel explainer",
    summary: "Move a discovered question through evidence, channel-specific writing, independent checking, visuals and exact-version approvals.",
    category: "FaktischSimpel workflow",
    page: "profiles",
    keywords: ["faktischsimpel", "explainer", "erklärvideo", "automation", "evidence", "prompt", "workflow"],
    steps: [
      "In Channels & subjects, confirm that FaktischSimpel Erklärvideo is enabled and the Themenradar schedule is active.",
      "In Research & evidence, review a finding in full. Shortlist only a question with a clear audience benefit and adequate source potential.",
      "Acquire sources, approve exact claims, counterevidence and unresolved questions, then approve the dossier version.",
      "Generate the script. The channel prompt enforces German step-by-step explanation; a separately routed model checks evidence, uncertainty and understandable structure.",
      "Approve the exact verified script, generate and review the explanatory storyboard, then approve the exact storyboard version.",
      "Produce media, resolve blocking QA findings, approve the exact render, upload privately and authorize publication separately.",
    ],
  },
  {
    id: "discover-shortlist",
    title: "Discover and shortlist a video opportunity",
    summary: "Run discovery, compare evidence potential and record an editorial decision.",
    category: "Research & evidence",
    page: "research",
    keywords: ["idea", "discovery", "opportunity", "shortlist", "defer", "reject", "research"],
    steps: [
      "Choose the intended channel and enabled subject before starting discovery.",
      "Run live discovery and follow its durable progress in Workflow activity.",
      "Select Review finding on an opportunity to open its complete, non-mutating review view.",
      "Inspect the original sources, score breakdown, grouping reasons, policy gates and estimated cost.",
      "Explain why the video deserves to exist and enter a concrete decision reason in the review view.",
      "Shortlist, defer or reject only after review; the decision and reason are retained in audit history.",
    ],
  },
  {
    id: "approve-dossier",
    title: "Review and approve a research dossier",
    summary: "Verify claims against immutable source snapshots before editorial work begins.",
    category: "Research & evidence",
    page: "research",
    keywords: ["dossier", "claim", "source", "evidence", "approve", "contradiction", "snapshot"],
    steps: [
      "Open a shortlisted opportunity and start or refresh its research dossier.",
      "Inspect every central claim, exact evidence passage and immutable source snapshot.",
      "Confirm that supporting sources are independent and review contradictory or limiting evidence.",
      "Resolve blockers, unsafe overstatements and unanswered questions rather than hiding them.",
      "Approve the exact dossier version only when its completion rules are met; otherwise reject it with a specific reason.",
    ],
  },
  {
    id: "recover-failed-research",
    title: "Recover failed research into a dossier",
    summary: "Manually review failed or incomplete research and create a normal review dossier from existing snapshots.",
    category: "Research & evidence",
    page: "research",
    roles: ["admin", "reviewer"],
    keywords: ["failed", "research", "manual", "recover", "dossier", "source", "snapshot", "approve", "edit"],
    steps: [
      "Open Research & evidence and select Review finding on an approved opportunity.",
      "Confirm that the finding has at least one immutable source snapshot; recovery does not fabricate evidence.",
      "Use Failed research recovery to edit the manual dossier summary, safe conclusions, limits and source-quality notes.",
      "Optionally paste claim lines and bind them to an exact excerpt from a selected source snapshot.",
      "Create the review dossier. The failed run remains in history and the new dossier appears in the normal review queue.",
      "Approve the recovered dossier only if the completion/readiness gates are satisfied, or record a reasoned override and resolve remaining blockers before script generation.",
    ],
  },
  {
    id: "script-storyboard",
    title: "Turn approved research into a script and storyboard",
    summary: "Generate cited narration, verify coverage and plan evidence-linked scenes.",
    category: "Scripts & storyboards",
    page: "editorial",
    keywords: ["script", "storyboard", "scene", "narration", "citation", "coverage", "lock"],
    steps: [
      "Select an approved dossier and generate a script draft.",
      "Review each segment's claim links, exact evidence and citation display.",
      "Fix verification issues and confirm that every factual statement has adequate coverage.",
      "Lock accepted script segments so later regeneration cannot silently replace them.",
      "Generate the storyboard, review each scene's purpose and source links, then lock accepted scenes.",
    ],
  },
  {
    id: "produce-review-media",
    title: "Produce and approve final media",
    summary: "Render from a locked storyboard, inspect quality findings and approve an exact output.",
    category: "Media & final review",
    page: "media",
    keywords: ["media", "render", "video", "voice", "narration", "quality", "qa", "caption", "approve"],
    steps: [
      "Choose the approved storyboard version, render tier, voice profile and active workflow registry entry.",
      "Start media production and monitor the durable workflow rather than submitting it again.",
      "Review the rendered video, narration auditions, captions, timing and source manifest.",
      "Inspect every quality finding; regenerate a bounded scene or narration segment when appropriate.",
      "Approve the exact render and manifest hashes only after all blocking findings are resolved.",
    ],
  },
  {
    id: "publish-video",
    title: "Upload and release a video safely",
    summary: "Prepare immutable metadata, upload privately and approve public release separately.",
    category: "Publishing & calendar",
    page: "publishing",
    keywords: ["youtube", "publish", "upload", "release", "metadata", "schedule", "private", "public"],
    steps: [
      "Confirm the intended channel workspace and its enabled YouTube connection.",
      "Select an approved render and create a versioned metadata record for title, description, chapters and disclosures.",
      "Review the render and metadata hashes before starting an upload.",
      "Upload in dry-run or private mode first and wait for processing, captions and thumbnail checks to finish.",
      "Use the separate release approval only after reviewing the exact uploaded asset, metadata version and schedule.",
    ],
  },
  {
    id: "recover-workflow",
    title: "Investigate or recover a failed workflow",
    summary: "Find the durable operation, diagnose its last state and retry without losing lineage.",
    category: "Workflow activity",
    page: "workflows",
    roles: ["admin", "operator"],
    keywords: ["failure", "failed", "retry", "stuck", "workflow", "recover", "error", "status"],
    steps: [
      "Open Workflow activity and filter to Needs attention in the correct channel.",
      "Find the workflow by task type, subject, channel or workflow ID.",
      "Open its stage and read the latest recorded failure instead of starting duplicate work.",
      "Correct the reported dependency, policy or input problem at that stage.",
      "Retry from the provided action; verify that the new workflow preserves its parent and correlation lineage.",
    ],
  },
  {
    id: "configure-model",
    title: "Configure a model for a task",
    summary: "Register a provider, version its prompt and activate a reviewed assignment.",
    category: "Providers & prompts",
    page: "providers",
    roles: ["admin"],
    keywords: ["ai", "model", "provider", "prompt", "assignment", "fallback", "budget", "secret"],
    steps: [
      "Register the provider endpoint and secret, then confirm its health before enabling it.",
      "Register the model with accurate context, output, capability and data-policy limits.",
      "Create or review the versioned prompt template and its input and response schemas.",
      "Create a task assignment with a primary model, ordered fallbacks and explicit budget policy.",
      "Activate the reviewed assignment version and confirm its audit entry before using it in production work.",
    ],
  },
  {
    id: "manage-user-access",
    title: "Create or change a user's access",
    summary: "Provision a local account and apply the least-privileged role needed for its work.",
    category: "Users",
    page: "users",
    roles: ["admin"],
    keywords: ["user", "account", "role", "permission", "password", "disable", "access"],
    steps: [
      "Open Users and confirm that an existing account does not already represent the person.",
      "Create the local username, display name and an initial password of at least eight characters.",
      "Assign the least-privileged role that covers the person's actual responsibilities.",
      "Ask the user to sign in and replace the initial password from Authentication.",
      "Disable the account promptly when access is no longer required; retain its audit history.",
    ],
  },
  {
    id: "secure-account",
    title: "Change your password or enable MFA",
    summary: "Strengthen a local account and store recovery codes safely.",
    category: "Authentication",
    page: "identity",
    keywords: ["security", "password", "mfa", "totp", "authenticator", "recovery", "code"],
    steps: [
      "Open Authentication and confirm whether the account is local or managed by an identity provider.",
      "For a local account, enter the current password and choose a new password with at least eight characters.",
      "To enable MFA, scan the setup code with an authenticator and confirm a current one-time code.",
      "Store the generated recovery codes in a secure location separate from this device.",
      "Test the new sign-in method before ending the current session.",
    ],
  },
  {
    id: "record-correction",
    title: "Record a source change or correction",
    summary: "Capture changed evidence and trace its effect on scripts and publications.",
    category: "Analytics & operations",
    page: "operations",
    roles: ["admin"],
    keywords: ["correction", "retraction", "source", "impact", "publication", "evidence", "change"],
    steps: [
      "Open Analytics & operations and identify the changed source, script or publication.",
      "Choose the change kind and severity based on the evidence, not the desired outcome.",
      "Describe what changed and include enough detail for another reviewer to reproduce the finding.",
      "Record the correction case to generate its deterministic impact map.",
      "Review every affected claim, timecode and publication, then complete the required release action.",
    ],
  },
];

function HelpPanel({ open, onClose, currentPage, role, onOpenPage }: { open: boolean; onClose: () => void; currentPage: string; role: Role; onOpenPage: (page: string) => void }) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState("");
  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open && !dialog.open) {
      dialog.showModal();
      window.requestAnimationFrame(() => searchRef.current?.focus());
    } else if (!open && dialog.open) dialog.close();
    if (!open) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      onClose();
    };
    document.addEventListener("keydown", closeOnEscape);
    return () => document.removeEventListener("keydown", closeOnEscape);
  }, [open]);
  const words = query.toLowerCase().trim().split(/\s+/).filter(Boolean);
  const results = helpTasks
    .filter(task => !task.roles || task.roles.includes(role))
    .filter(task => {
      if (!words.length) return true;
      const searchable = [task.title, task.summary, task.category, ...task.keywords, ...task.steps].join(" ").toLowerCase();
      return words.every(word => searchable.includes(word));
    })
    .sort((left, right) => Number(right.page === currentPage) - Number(left.page === currentPage) || left.title.localeCompare(right.title));
  const openTaskPage = (task: HelpTask) => { onOpenPage(task.page); onClose(); };
  return <dialog ref={dialogRef} id="task-help-panel" className="help-panel" aria-labelledby="task-help-title" onCancel={event => { event.preventDefault(); onClose(); }} onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}>
    <div className="help-panel-shell">
      <header className="help-panel-header"><div><p className="eyebrow">Task-based guidance</p><h2 id="task-help-title">How can we help?</h2><p>Search for a goal, then follow the procedure in order.</p></div><button type="button" className="help-close" data-usage="Close task help" aria-label="Close task help" onClick={onClose}>×</button></header>
      <label className="help-search" data-usage="Search task procedures"><span>Search procedures</span><input ref={searchRef} type="search" value={query} onChange={event => setQuery(event.target.value)} placeholder="Try “publish”, “failed workflow” or “password”" aria-description="Search task titles, summaries, keywords and individual steps." /></label>
      <div className="help-result-summary" role="status" aria-live="polite"><strong>{results.length}</strong> {results.length === 1 ? "procedure" : "procedures"}{query.trim() ? ` matching “${query.trim()}”` : " available for your role"}</div>
      <div className="help-task-list">
        {results.map(task => <details className="help-task" key={task.id}>
          <summary><span><small>{task.category}{task.page === currentPage ? " · Current stage" : ""}</small><strong>{task.title}</strong><em>{task.summary}</em></span></summary>
          <div className="help-task-body"><ol>{task.steps.map((step, stepIndex) => <li key={`${task.id}-${stepIndex}`}>{step}</li>)}</ol><button type="button" className="secondary compact" data-usage={`Open ${task.category}`} onClick={() => openTaskPage(task)}>Open {task.category}</button></div>
        </details>)}
        {!results.length && <div className="help-empty"><strong>No matching procedure</strong><p>Try a broader task word such as “research”, “publish”, “user” or “workflow”.</p><button type="button" className="secondary compact" data-usage="Clear help search" onClick={() => { setQuery(""); searchRef.current?.focus(); }}>Clear search</button></div>}
      </div>
    </div>
  </dialog>;
}

function randomUuid(): string {
  if (typeof globalThis.crypto?.randomUUID === "function") return globalThis.crypto.randomUUID();
  const bytes = new Uint8Array(16);
  if (typeof globalThis.crypto?.getRandomValues === "function") globalThis.crypto.getRandomValues(bytes);
  else for (let index = 0; index < bytes.length; index += 1) bytes[index] = Math.floor(Math.random() * 256);
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const value = Array.from(bytes, byte => byte.toString(16).padStart(2, "0")).join("");
  return `${value.slice(0, 8)}-${value.slice(8, 12)}-${value.slice(12, 16)}-${value.slice(16, 20)}-${value.slice(20)}`;
}

function safeExternalUrl(value: string): string | null {
  try {
    const parsed = new URL(value);
    return parsed.protocol === "https:" || parsed.protocol === "http:" ? parsed.href : null;
  } catch {
    return null;
  }
}

function ExternalSourceLink({ url, title }: { url: string; title: string }) {
  const safeUrl = safeExternalUrl(url);
  return safeUrl ? <a href={safeUrl} target="_blank" rel="noopener noreferrer">{title}</a> : <span>{title}</span>;
}

function readableDetailName(value: string): string {
  return value.replaceAll("_", " ").replace(/\b\w/g, letter => letter.toUpperCase());
}

function readableDetailValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "Not recorded";
  if (Array.isArray(value)) return value.length ? value.map(readableDetailValue).join(", ") : "None";
  if (typeof value === "object") return JSON.stringify(value);
  if (typeof value === "boolean") return value ? "Yes" : "No";
  return String(value);
}

function DetailRecord({ values, empty = "Nothing recorded" }: { values: Record<string, unknown>; empty?: string }) {
  const entries = Object.entries(values);
  if (!entries.length) return <p className="muted">{empty}</p>;
  return <dl className="finding-record">{entries.map(([name, value]) => <div key={name}><dt>{readableDetailName(name)}</dt><dd>{readableDetailValue(value)}</dd></div>)}</dl>;
}

function OpportunityReviewDialog({
  opportunity, role, csrf, decisionReason, rationale, onDecisionReasonChange, onRationaleChange, onClose, onDecision, onManualDossierCreated,
}: {
  opportunity: Opportunity;
  role: Role;
  csrf: string;
  decisionReason: string;
  rationale: string;
  onDecisionReasonChange: (value: string) => void;
  onRationaleChange: (value: string) => void;
  onClose: () => void;
  onDecision: (item: Opportunity, decision: "approved" | "rejected" | "deferred") => void;
  onManualDossierCreated: (dossier: Dossier) => void | Promise<void>;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [sources, setSources] = useState<SourceBrowserItem[]>([]);
  const [sourcesLoading, setSourcesLoading] = useState(true);
  const [sourceMessage, setSourceMessage] = useState("");
  const [preview, setPreview] = useState<SourcePreview | null>(null);
  const [manualSummary, setManualSummary] = useState(opportunity.summary);
  const [manualConclusions, setManualConclusions] = useState(opportunity.summary);
  const [manualQuestions, setManualQuestions] = useState("Welche Aussagen bleiben unsicher oder quellenabhängig?");
  const [manualAlternatives, setManualAlternatives] = useState("");
  const [manualSourceNotes, setManualSourceNotes] = useState("Manual recovery after failed or incomplete automated research; sources were reviewed by a human.");
  const [manualOverstatements, setManualOverstatements] = useState("Nicht stärker formulieren, als es die geprüften Quellen stützen.");
  const [manualClaims, setManualClaims] = useState("");
  const [manualSnapshotId, setManualSnapshotId] = useState("");
  const [manualExactText, setManualExactText] = useState("");
  const [manualCounterevidenceChecked, setManualCounterevidenceChecked] = useState(false);
  const [manualReviewNote, setManualReviewNote] = useState("Human recovery review: failed automated research was manually checked and converted into a dossier for formal review.");
  const [manualMessage, setManualMessage] = useState("");
  const [manualBusy, setManualBusy] = useState(false);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    const previouslyFocused = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    dialog.showModal();
    return () => { if (dialog.open) dialog.close(); previouslyFocused?.focus(); };
  }, []);

  useEffect(() => {
    let cancelled = false;
    setSourcesLoading(true);
    setSourceMessage("");
    api<SourceBrowserItem[]>(`/api/v1/research/sources?opportunity_id=${encodeURIComponent(opportunity.id)}`)
      .then(next => { if (!cancelled) setSources(next); })
      .catch(caught => { if (!cancelled) setSourceMessage(caught instanceof Error ? caught.message : "Sources could not be loaded"); })
      .finally(() => { if (!cancelled) setSourcesLoading(false); });
    return () => { cancelled = true; };
  }, [opportunity.id]);

  useEffect(() => {
    setManualSummary(opportunity.summary);
    setManualConclusions(opportunity.summary);
    setManualQuestions("Welche Aussagen bleiben unsicher oder quellenabhängig?");
    setManualAlternatives("");
    setManualSourceNotes("Manual recovery after failed or incomplete automated research; sources were reviewed by a human.");
    setManualOverstatements("Nicht stärker formulieren, als es die geprüften Quellen stützen.");
    setManualClaims("");
    setManualSnapshotId("");
    setManualExactText("");
    setManualCounterevidenceChecked(false);
    setManualReviewNote("Human recovery review: failed automated research was manually checked and converted into a dossier for formal review.");
    setManualMessage("");
  }, [opportunity.id, opportunity.summary]);

  useEffect(() => {
    const firstSnapshot = sources.flatMap(source => source.snapshots)[0];
    setManualSnapshotId(current => current || firstSnapshot?.id || "");
  }, [sources]);

  async function showPreview(sourceId: string) {
    setSourceMessage("");
    try { setPreview(await api<SourcePreview>(`/api/v1/research/sources/${sourceId}/preview`)); }
    catch (caught) { setSourceMessage(caught instanceof Error ? caught.message : "Source preview failed"); }
  }

  const canReview = (role === "admin" || role === "reviewer") && (opportunity.decision === "pending" || opportunity.decision === "deferred");
  const canRecoverResearch = (role === "admin" || role === "reviewer") && opportunity.decision === "approved";
  const snapshotOptions = sources.flatMap(source => source.snapshots.map(snapshot => ({ snapshot, source })));
  const effectiveRationale = opportunity.editorial_rationale || rationale;
  const decisionDisabled = decisionReason.trim().length < 3;
  const shortlistDisabled = decisionDisabled || effectiveRationale.trim().length < 20;
  const manualConclusionsReady = linesFromText(manualConclusions).length > 0;
  const manualDisabled = manualBusy || !snapshotOptions.length || manualSummary.trim().length < 20 || !manualConclusionsReady || manualReviewNote.trim().length < 10;

  async function createManualDossier(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setManualMessage("");
    const safeConclusions = linesFromText(manualConclusions);
    if (!safeConclusions.length) {
      setManualMessage("At least one safe conclusion is required.");
      return;
    }
    const exactText = manualExactText.trim();
    const useExcerpt = Boolean(manualSnapshotId && exactText);
    const claims = linesFromText(manualClaims).map(statement => ({
      statement,
      claim_type: "fact",
      confidence: 70,
      risk: "medium",
      central: true,
      source_snapshot_id: useExcerpt ? manualSnapshotId : null,
      exact_text: useExcerpt ? exactText : null,
      relationship: "supports",
      source_independent: true,
      direct_evidence: true,
      primary_source: false,
      coverage_unit_ids: [],
    }));
    setManualBusy(true);
    try {
      const dossier = await api<Dossier>(`/api/v1/research/opportunities/${opportunity.id}/manual-dossier`, {
        method: "POST",
        body: JSON.stringify({
          expected_opportunity_version: opportunity.version,
          idempotency_key: `ui-${randomUuid()}`,
          executive_summary: manualSummary,
          safe_conclusions: safeConclusions,
          unresolved_questions: linesFromText(manualQuestions),
          alternative_explanations: linesFromText(manualAlternatives),
          source_quality_notes: linesFromText(manualSourceNotes),
          prohibited_overstatements: linesFromText(manualOverstatements),
          proposed_angles: [opportunity.title],
          counterevidence_search_completed: manualCounterevidenceChecked,
          review_note: manualReviewNote,
          claims,
        }),
      }, csrf);
      setManualMessage(`Manual dossier v${dossier.dossier_version} created for review.`);
      await onManualDossierCreated(dossier);
    } catch (caught) {
      setManualMessage(caught instanceof Error ? caught.message : "Manual dossier creation failed");
    } finally {
      setManualBusy(false);
    }
  }

  return <dialog
    ref={dialogRef}
    id="opportunity-review-dialog"
    className="finding-dialog"
    aria-labelledby="opportunity-review-title"
    onCancel={event => { event.preventDefault(); onClose(); }}
    onClick={event => { if (event.target === dialogRef.current) onClose(); }}
  >
    <div className="finding-dialog-shell">
      <header className="finding-dialog-header">
        <div><p className="eyebrow">Full finding review</p><h2 id="opportunity-review-title">{opportunity.title}</h2><div className="finding-header-meta"><span className={`status ${opportunity.decision === "approved" ? "good" : opportunity.decision === "rejected" ? "bad" : "waiting"}`}>{opportunity.decision}</span><span>Created {new Date(opportunity.created_at).toLocaleString()}</span><code>{opportunity.id}</code></div></div>
        <button type="button" className="help-close" aria-label="Close finding review" onClick={onClose}>×</button>
      </header>
      <div className="finding-dialog-content">
        <main className="finding-detail-main">
          <section className="finding-section"><p className="eyebrow">Finding</p><h3>Complete summary</h3><p className="finding-summary">{opportunity.summary}</p><h3>Why this video deserves to exist</h3><p>{opportunity.editorial_rationale || "No editorial rationale has been recorded yet."}</p></section>

          <section className="finding-section">
            <div className="panel-title"><div><p className="eyebrow">Original material</p><h3>Sources</h3></div><span className="chip">{sources.length || opportunity.source_count}</span></div>
            {sourceMessage && <div className="notice error">{sourceMessage}</div>}
            {sourcesLoading && <p className="muted">Loading source records…</p>}
            {!sourcesLoading && !sources.length && <p className="empty">No source records are attached to this finding.</p>}
            <div className="finding-source-list">{sources.map(source => <article className="finding-source" key={source.id}><div><ExternalSourceLink url={source.canonical_url} title={source.title} /><small>{source.publisher || source.domain} · {readableDetailName(source.source_type)}{source.publication_at ? ` · ${new Date(source.publication_at).toLocaleDateString()}` : ""}</small><small>{source.author ? `By ${source.author} · ` : ""}{source.snapshots.length} immutable snapshot{source.snapshots.length === 1 ? "" : "s"}</small></div><div className="actions"><ExternalSourceLink url={source.canonical_url} title="Open original ↗" /><button type="button" className="secondary compact" disabled={!source.snapshots.length} title={source.snapshots.length ? "Read the normalized immutable snapshot" : "A text preview becomes available after source acquisition"} onClick={() => showPreview(source.id)}>Preview text</button></div></article>)}</div>
            {!sourcesLoading && sources.length > 0 && sources.every(source => !source.snapshots.length) && <div className="notice">Full article text has not been acquired yet. Open the original source to review it now; an immutable text preview becomes available after the opportunity is shortlisted and its sources are acquired.</div>}
            {preview && <div className="finding-preview"><div className="panel-title"><strong>Untrusted normalized source text{preview.truncated ? " · truncated" : ""}</strong><button type="button" className="secondary compact" onClick={() => setPreview(null)}>Close preview</button></div><pre>{preview.text}</pre><small>SHA-256 {preview.content_hash}</small></div>}
          </section>

          <section className="finding-section"><p className="eyebrow">Discovery trace</p><h3>Grouping and classification reasons</h3>{opportunity.grouping_reason.length ? <ul className="finding-reason-list">{opportunity.grouping_reason.map((reason, index) => <li key={`${reason}-${index}`}>{reason}</li>)}</ul> : <p className="muted">No grouping reasons were recorded.</p>}</section>

          <section className="finding-section"><p className="eyebrow">Scoring trace {opportunity.score_version ? `· version ${opportunity.score_version}` : ""}</p><h3>Why this finding received {opportunity.score === null ? "no score" : `${opportunity.score}/100`}</h3><div className="finding-score-grid"><div><h4>Observed components</h4><DetailRecord values={opportunity.score_components} /></div><div><h4>Penalties</h4><DetailRecord values={opportunity.score_penalties} empty="No penalties applied" /></div><div><h4>Configured weights</h4><DetailRecord values={opportunity.score_weights} empty="Default equal weights" /></div></div>{opportunity.score_reasoning.length ? <ul className="finding-reason-list">{opportunity.score_reasoning.map((reason, index) => <li key={`${reason}-${index}`}>{reason}</li>)}</ul> : <p className="muted">No score narrative was recorded.</p>}</section>
          {opportunity.ai_qualification && <section className="finding-section"><p className="eyebrow">AI topic qualification · advisory v{opportunity.ai_qualification.version}</p><h3>{opportunity.ai_qualification.abstained ? "Model abstained" : `${Math.round(opportunity.ai_qualification.confidence * 100)}% confidence`}</h3><p className="muted">The model did not supply the total. Hard evidence, duplication and risk gates remain deterministic.</p><DetailRecord values={opportunity.ai_qualification.dimensions} />{opportunity.ai_qualification.rationale.length > 0 && <ul className="finding-reason-list">{opportunity.ai_qualification.rationale.map(value => <li key={value}>{value}</li>)}</ul>}{opportunity.ai_qualification.uncertainty.length > 0 && <details><summary>Uncertainty and abstention reasons</summary><ul>{opportunity.ai_qualification.uncertainty.map(value => <li key={value}>{value}</li>)}</ul></details>}</section>}
        </main>

        <div className="finding-detail-aside">
          <section className="finding-section"><p className="eyebrow">Workflow state</p><DetailRecord values={{ decision: opportunity.decision, research_state: opportunity.research_state, sources: opportunity.source_count, immutable_snapshots: opportunity.snapshot_count, record_version: opportunity.version }} /></section>
          <section className="finding-section"><p className="eyebrow">Policy snapshot</p><DetailRecord values={opportunity.policy_snapshot} /></section>
          <section className="finding-section"><p className="eyebrow">Estimated cost</p><DetailRecord values={opportunity.estimated_cost || {}} empty="No cost estimate recorded" /></section>
          {canReview && <section className="finding-section finding-decision"><p className="eyebrow">Human decision gate</p><h3>Record a reviewed decision</h3><label>Decision reason<input value={decisionReason} minLength={3} onChange={event => onDecisionReasonChange(event.target.value)} /></label>{!opportunity.editorial_rationale && <label>Why does this video deserve to exist?<textarea rows={4} value={rationale} minLength={20} onChange={event => onRationaleChange(event.target.value)} /></label>}<p className="muted">This action is version-checked and retained in audit history.</p><div className="actions"><button type="button" className="primary compact" disabled={shortlistDisabled} onClick={() => onDecision(opportunity, "approved")}>Shortlist</button><button type="button" className="secondary compact" disabled={decisionDisabled} onClick={() => onDecision(opportunity, "deferred")}>Defer</button><button type="button" className="secondary compact danger" disabled={decisionDisabled} onClick={() => onDecision(opportunity, "rejected")}>Reject</button></div></section>}
          {opportunity.decision === "approved" && <section className="finding-section"><p className="eyebrow">Automatic continuation</p><h3>Sources and dossier continue without another click</h3><p className="muted">The approval starts durable source acquisition and then the evidence dossier. Progress and retry controls are available in Workflow activity.</p></section>}
          {canRecoverResearch && <section className="finding-section finding-decision"><p className="eyebrow">Failed research recovery</p><h3>Manually create a review dossier</h3><p className="muted">Use this when automated research failed or produced incomplete output. The dossier enters the normal review queue; script generation still requires the usual explanation-readiness gates.</p>{manualMessage && <div className={`notice ${manualMessage.includes("failed") || manualMessage.includes("required") || manualMessage.includes("available") ? "error" : ""}`}>{manualMessage}</div>}<form onSubmit={createManualDossier}><label>Manual dossier summary<textarea rows={5} value={manualSummary} minLength={20} onChange={event => setManualSummary(event.target.value)} required /></label><label>Safe conclusions<textarea rows={4} value={manualConclusions} minLength={10} onChange={event => setManualConclusions(event.target.value)} placeholder="One supported conclusion per line" required /></label><label>Unresolved questions / limits<textarea rows={3} value={manualQuestions} onChange={event => setManualQuestions(event.target.value)} placeholder="One limitation or open question per line" /></label><label>Alternative explanations / context<textarea rows={3} value={manualAlternatives} onChange={event => setManualAlternatives(event.target.value)} placeholder="One alternative interpretation per line" /></label><label>Source-quality notes<textarea rows={3} value={manualSourceNotes} onChange={event => setManualSourceNotes(event.target.value)} /></label><label>Prohibited overstatements<textarea rows={3} value={manualOverstatements} onChange={event => setManualOverstatements(event.target.value)} /></label><label>Optional claim statements<textarea rows={5} value={manualClaims} onChange={event => setManualClaims(event.target.value)} placeholder="One claim per line. Claims with an excerpt become supported; claims without one remain draft." /></label><label>Source snapshot for excerpt<select value={manualSnapshotId} disabled={!snapshotOptions.length} onChange={event => setManualSnapshotId(event.target.value)}><option value="">Select acquired source snapshot</option>{snapshotOptions.map(({ source, snapshot }) => <option key={snapshot.id} value={snapshot.id}>{source.title} · snapshot {snapshot.snapshot_number} · {snapshot.content_hash.slice(0, 10)}…</option>)}</select></label><label>Exact supporting excerpt<textarea rows={4} value={manualExactText} disabled={!manualSnapshotId} onChange={event => setManualExactText(event.target.value)} placeholder="Paste the exact source text that supports the optional claim lines." /></label><label><input type="checkbox" checked={manualCounterevidenceChecked} onChange={event => setManualCounterevidenceChecked(event.target.checked)} /> Counterevidence / limits were checked manually</label><label>Recovery review note<input value={manualReviewNote} minLength={10} onChange={event => setManualReviewNote(event.target.value)} required /></label><button type="submit" className="primary compact control-help" data-usage="Create a new in-review dossier from manually checked failed research. The original failed run remains in history." disabled={manualDisabled}>{manualBusy ? "Creating…" : "Create review dossier"}</button>{!snapshotOptions.length && <p className="muted">Source acquisition must have at least one immutable snapshot before manual recovery can create a dossier.</p>}</form></section>}
        </div>
      </div>
    </div>
  </dialog>;
}

function SourceBrowser({ opportunityId, csrf, role }: { opportunityId: string; csrf: string; role: Role }) {
  const [sources, setSources] = useState<SourceBrowserItem[]>([]);
  const [preview, setPreview] = useState<SourcePreview | null>(null);
  const [message, setMessage] = useState("");
  const [fromSource, setFromSource] = useState("");
  const [toSource, setToSource] = useState("");
  const [relationship, setRelationship] = useState("derived_from");
  const [reason, setReason] = useState("Editorial review identified a shared upstream source or dependency.");
  const refresh = useCallback(async () => {
    try {
      const next = await api<SourceBrowserItem[]>(`/api/v1/research/sources?opportunity_id=${encodeURIComponent(opportunityId)}`);
      setSources(next); setFromSource(current => current || next[0]?.id || ""); setToSource(current => current || next[1]?.id || "");
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Sources could not be loaded"); }
  }, [opportunityId]);
  useEffect(() => { refresh(); }, [refresh]);
  async function showPreview(sourceId: string) {
    try { setPreview(await api<SourcePreview>(`/api/v1/research/sources/${sourceId}/preview`)); }
    catch (caught) { setMessage(caught instanceof Error ? caught.message : "Source preview failed"); }
  }
  async function indexSource(sourceId: string) {
    try {
      const run = await api<{ workflow_id: string; state: string }>(`/api/v1/research/sources/${sourceId}/index`, { method: "POST", body: JSON.stringify({ idempotency_key: `ui-${randomUuid()}` }) }, csrf);
      setMessage(`Semantic indexing started: ${run.workflow_id}. Refresh this dossier after it reaches indexed.`);
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Source indexing failed"); }
  }
  async function createRelationship() {
    try {
      await api("/api/v1/research/source-relationships", { method: "POST", body: JSON.stringify({ source_document_id: fromSource, related_source_document_id: toSource, relationship, reason, confidence: 90 }) }, csrf);
      setMessage("Source lineage recorded for future independence evaluation."); await refresh();
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Source lineage could not be recorded"); }
  }
  return <section className="source-browser"><div className="panel-title"><div><p className="eyebrow">Immutable source browser</p><h2>Acquired evidence</h2></div><span className="chip">{sources.length}</span></div>{message && <div className="notice">{message}</div>}<div className="card-list">{sources.map(source => <article className="profile-card subject" key={source.id}><div><ExternalSourceLink url={source.canonical_url} title={source.title} /><small>{source.domain} · {source.source_type} · {source.snapshots.length} snapshot{source.snapshots.length === 1 ? "" : "s"}</small>{source.snapshots[0] && <small>{source.snapshots[0].semantic_chunk_count} semantic chunks · SHA-256 {source.snapshots[0].content_hash.slice(0, 16)}…</small>}{source.relationships.map(item => <small key={item.id}>{item.relationship} · {item.confidence}% · {item.reason}</small>)}</div><div className="actions"><button className="secondary compact" disabled={!source.snapshots.length} onClick={() => showPreview(source.id)}>Preview text</button>{(role === "admin" || role === "operator") && source.snapshots[0] && source.snapshots[0].semantic_chunk_count === 0 && <button className="secondary compact" onClick={() => indexSource(source.id)}>Index source</button>}</div></article>)}</div>{(role === "admin" || role === "editor") && sources.length > 1 && <div className="review-bar"><label>Source<select value={fromSource} onChange={event => setFromSource(event.target.value)}>{sources.map(source => <option key={source.id} value={source.id}>{source.title}</option>)}</select></label><label>Related source<select value={toSource} onChange={event => setToSource(event.target.value)}>{sources.map(source => <option key={source.id} value={source.id}>{source.title}</option>)}</select></label><label>Lineage<select value={relationship} onChange={event => setRelationship(event.target.value)}><option value="derived_from">derived from</option><option value="repeats">repeats</option><option value="cites">cites</option><option value="near_duplicate">near duplicate</option><option value="independent">independent</option></select></label><label>Reason<input value={reason} onChange={event => setReason(event.target.value)} minLength={10} /></label><button className="secondary" disabled={!fromSource || !toSource || fromSource === toSource} onClick={createRelationship}>Record lineage</button></div>}{preview && <div className="notice"><div className="panel-title"><strong>Untrusted normalized source text{preview.truncated ? " · truncated" : ""}</strong><button className="secondary compact" onClick={() => setPreview(null)}>Close preview</button></div><pre>{preview.text}</pre></div>}</section>;
}

function AuthScreen({ bootstrap, onSession }: { bootstrap: boolean; onSession: (s: Session) => void }) {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [showPassword, setShowPassword] = useState(false);
  const [oidc, setOidc] = useState<{ enabled: boolean; issuer: string | null }>({ enabled: false, issuer: null });
  useEffect(() => { api<{ enabled: boolean; issuer: string | null }>("/api/v1/auth/oidc/status").then(setOidc).catch(() => undefined); }, []);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    const data = new FormData(event.currentTarget);
    const payload = Object.fromEntries(data.entries());
    payload.username = String(payload.username || "").trim().toLowerCase();
    if (!payload.totp_code) delete payload.totp_code;
    try {
      const session = await api<Session>(`/api/v1/auth/${bootstrap ? "bootstrap" : "login"}`, {
        method: "POST",
        body: JSON.stringify(payload),
      });
      onSession(session);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Authentication failed");
    } finally {
      setBusy(false);
    }
  }

  async function startOidc() {
    setBusy(true); setError("");
    try {
      const result = await api<{ authorization_url: string }>("/api/v1/auth/oidc/start");
      window.location.assign(result.authorization_url);
    } catch (caught) { setError(caught instanceof Error ? caught.message : "OIDC authentication failed"); setBusy(false); }
  }

  return (
    <main className="auth-shell">
      <section className="auth-story">
        <div className="brand-mark">TF</div>
        <p className="eyebrow">TubeFactory</p>
        <h1>Editorial judgment, backed by a visible chain of evidence.</h1>
        <p className="lede">Research, review, production and publication stay recoverable—and under human control.</p>
        <div className="principle-list">
          <span>Trace every factual sentence</span><span>Review every consequential transition</span><span>Publish privately first</span>
        </div>
      </section>
      <section className="auth-panel">
        <div className="auth-card">
          <p className="eyebrow">{bootstrap ? "First-time setup" : "Operator access"}</p>
          <h2>{bootstrap ? "Create the administrator" : "Welcome back"}</h2>
          <p className="muted">{bootstrap ? "This one-time gate closes after the account is committed." : "Use your local editorial account."}</p>
          <form onSubmit={submit}>
            {bootstrap && <label>Display name<input name="display_name" required autoComplete="name" /></label>}
            <label>Username<input name="username" minLength={3} required autoComplete="username" /></label>
            <label>Password<input name="password" type={showPassword ? "text" : "password"} minLength={8} required autoComplete={bootstrap ? "new-password" : "current-password"} /></label>
            <label className="checkbox"><input type="checkbox" checked={showPassword} onChange={event => setShowPassword(event.target.checked)} /> Show password</label>
            {!bootstrap && <label>Authenticator or recovery code <span className="muted">(if enabled)</span><input name="totp_code" autoComplete="one-time-code" pattern="[A-Za-z0-9-]+" /></label>}
            {error && <div className="notice error">{error}</div>}
            <button className="primary" disabled={busy}>{busy ? "Working…" : bootstrap ? "Create administrator" : "Sign in"}</button>
          </form>
          {!bootstrap && oidc.enabled && <><div className="separator"><span>or</span></div><button className="secondary" disabled={busy} onClick={startOidc}>Continue with configured identity provider</button><small className="muted">Issuer: {oidc.issuer}</small></>}
        </div>
      </section>
    </main>
  );
}

function Dashboard({ capabilities, activeChannelId }: { capabilities: Capabilities | null; activeChannelId: string }) {
  const [workflows, setWorkflows] = useState<WorkflowSummary[]>([]);
  useEffect(() => { api<WorkflowSummary[]>("/api/v1/system/workflows?limit=100").then(setWorkflows).catch(() => undefined); }, []);
  const scopedWorkflows = activeChannelId ? workflows.filter(item => item.channel_profile_id === activeChannelId) : workflows;
  const attention = scopedWorkflows.filter(item => ["FAILED", "CANCELLED", "TERMINATED", "TIMED_OUT", "UNKNOWN"].includes(item.execution_status)).length;
  return <>
    <div className="page-heading"><div><p className="eyebrow">Operations overview</p><h1>Production control</h1></div><span className="status good">Core online</span></div>
    <div className="metric-grid">
      <article className="metric"><span>Production stages</span><strong>13</strong><small>Discovery through measured publication online</small></article>
      <article className="metric"><span>Active workflows</span><strong>{scopedWorkflows.filter(item => item.execution_status === "RUNNING").length}</strong><small>Currently running in this workspace</small></article>
      <article className="metric"><span>Needs attention</span><strong>{attention}</strong><small>Failed, cancelled or unavailable workflow history</small></article>
      <article className="metric"><span>Enabled capabilities</span><strong>{capabilities ? Object.values(capabilities.capabilities).filter(v => v.available).length : "—"}</strong><small>Verified service contracts</small></article>
    </div>
    <section className="panel"><div className="panel-title"><div><p className="eyebrow">Delivery map</p><h2>Capabilities unlock only after verification</h2></div></div>
      <div className="roadmap-grid">{roadmap.map(([name, description, increment]) => <article className="roadmap" key={name}><div><h3>{name}</h3><p>{description}</p></div><span className="chip good">Increment {increment} · online</span></article>)}</div>
    </section>
  </>;
}

function ConfigPanel({ csrf }: { csrf: string }) {
  const [items, setItems] = useState<ConfigVersion[]>([]);
  const [namespace, setNamespace] = useState("system.editorial");
  const [document, setDocument] = useState('{\n  "publishing_enabled": false,\n  "default_language": "en"\n}');
  const [message, setMessage] = useState("");
  const refresh = useCallback(() => api<ConfigVersion[]>("/api/v1/configuration").then(setItems).catch(e => setMessage(e.message)), []);
  useEffect(() => { refresh(); }, [refresh]);
  const selectedVersion = items.find(item => item.namespace === namespace);
  let parsedDocument: Record<string, unknown> | null = null;
  let jsonError = "";
  try {
    const parsed = JSON.parse(document) as unknown;
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) jsonError = "Document must be a JSON object, not an array or scalar value.";
    else parsedDocument = parsed as Record<string, unknown>;
  } catch (caught) { jsonError = caught instanceof Error ? caught.message : "JSON could not be parsed."; }
  const documentChanged = selectedVersion ? JSON.stringify(selectedVersion.document, null, 2) !== document : true;
  async function save(e: FormEvent) {
    e.preventDefault(); setMessage("");
    try {
      if (!parsedDocument) throw new Error(jsonError || "Document must be valid JSON before activation.");
      await api("/api/v1/configuration", { method: "PUT", body: JSON.stringify({ namespace, schema_version: "1.0", document: parsedDocument, comment: "Edited in operator UI" }) }, csrf);
      setMessage("A new immutable configuration version is active."); refresh();
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Save failed"); }
  }
  return <>
    <div className="page-heading"><div><p className="eyebrow">Versioned settings</p><h1>Configuration</h1><p className="muted">Select a namespace, edit valid JSON, then activate a new immutable version.</p></div><span className="chip">{items.length} active namespace{items.length === 1 ? "" : "s"}</span></div>
    <div className="split-grid"><section className="panel"><div className="panel-title"><div><h2>Active namespaces</h2><p className="muted">Choose a namespace to load its active version into the editor.</p></div></div>{items.length ? items.map(item => <button className="config-row" key={item.id} onClick={() => { setNamespace(item.namespace); setDocument(JSON.stringify(item.document, null, 2)); setMessage(""); }}><span><strong>{item.namespace}</strong><small>Schema {item.schema_version} · v{item.version} · {new Date(item.created_at).toLocaleString()}</small></span><span className="status good">Active</span></button>) : <p className="empty">No configuration has been created.</p>}</section>
    <section className="panel"><div className="panel-title"><div><h2>Edit JSON document</h2><p className="muted">Activation creates a new version; it does not mutate the previous one.</p></div><span className={`json-status ${parsedDocument ? "good" : "bad"}`}>{parsedDocument ? "Valid JSON" : "Invalid JSON"}</span></div><form onSubmit={save}><label>Namespace<input value={namespace} onChange={e => setNamespace(e.target.value)} pattern="[a-z][a-z0-9_.-]*" required /></label><label>Document<textarea value={document} onChange={e => setDocument(e.target.value)} rows={14} spellCheck={false} /></label>{jsonError && <div className="notice error">Fix JSON before activation: {jsonError}</div>}<div className="config-safety"><span><strong>{selectedVersion ? `Current active version: v${selectedVersion.version}` : "New namespace"}</strong><small>{selectedVersion ? `Hash ${selectedVersion.document_hash.slice(0, 12)} · ${selectedVersion.comment || "No comment"}` : "No active version exists yet."}</small></span><span className={`status ${documentChanged ? "waiting" : "good"}`}>{documentChanged ? "Draft differs" : "No changes"}</span></div>{message && <div className={`notice ${message.toLowerCase().includes("failed") || message.toLowerCase().includes("invalid") ? "error" : ""}`}>{message}</div>}<div className="actions"><button className="primary" disabled={!parsedDocument || !documentChanged}>Create & activate version</button><a className="secondary" href={`/api/v1/configuration/${namespace}/export?format=yaml`}>Export active YAML</a></div></form></section></div>
  </>;
}

function UsersPanel({ csrf }: { csrf: string }) {
  const [users, setUsers] = useState<User[]>([]); const [message, setMessage] = useState("");
  const [query, setQuery] = useState("");
  const [roleFilter, setRoleFilter] = useState<Role | "all">("all");
  const refresh = useCallback(() => api<User[]>("/api/v1/users").then(setUsers).catch(e => setMessage(e.message)), []);
  useEffect(() => { refresh(); }, [refresh]);
  async function create(e: FormEvent<HTMLFormElement>) { e.preventDefault(); const form = e.currentTarget; const payload = Object.fromEntries(new FormData(form).entries()); try { await api("/api/v1/users", { method: "POST", body: JSON.stringify(payload) }, csrf); form.reset(); setMessage("User created."); refresh(); } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Create failed"); } }
  const normalizedQuery = query.trim().toLowerCase();
  const roles: Role[] = ["admin", "operator", "editor", "reviewer", "viewer"];
  const visibleUsers = users.filter(user => (roleFilter === "all" || user.role === roleFilter) && (!normalizedQuery || [user.display_name, user.username, user.role, user.identity_provider].some(value => value.toLowerCase().includes(normalizedQuery))));
  return <><div className="page-heading"><div><p className="eyebrow">Access control</p><h1>Users and roles</h1><p className="muted">Find existing accounts before creating another local login.</p></div><span className="chip">{users.filter(user => user.enabled).length} enabled of {users.length}</span></div><div className="split-grid"><section className="panel"><div className="panel-title"><div><h2>Accounts</h2><p className="muted">{visibleUsers.length} account{visibleUsers.length === 1 ? "" : "s"} {visibleUsers.length === 1 ? "matches" : "match"} the current filters.</p></div></div><div className="list-toolbar"><label>Find account<input type="search" value={query} onChange={event => setQuery(event.target.value)} placeholder="Name, username, role or identity source" /></label><label>Role<select value={roleFilter} onChange={event => setRoleFilter(event.target.value as Role | "all")}><option value="all">All roles</option>{roles.map(role => <option key={role} value={role}>{role[0].toUpperCase() + role.slice(1)}</option>)}</select></label></div><div className="table-list compact-list">{visibleUsers.map(user => <div className="user-row" key={user.id}><span className="avatar">{user.display_name.slice(0, 2).toUpperCase()}</span><span><strong>{user.display_name}</strong><small>@{user.username} · {user.identity_provider} · v{user.version}</small></span><span className="chip">{user.role}</span><span className={`status ${user.enabled ? "good" : ""}`}>{user.enabled ? "Enabled" : "Disabled"}</span></div>)}{!visibleUsers.length && <p className="empty">No accounts match this search and role filter.</p>}</div></section><section className="panel"><h2>Add local account</h2><p className="muted">Create accounts only for people or services that need direct operator access.</p><form onSubmit={create}><label>Display name<input name="display_name" required /></label><label>Username<input name="username" minLength={3} pattern="[a-zA-Z0-9_.-]+" required /></label><label>Temporary password<input name="password" type="password" minLength={8} required /></label><label>Role<select name="role" defaultValue="viewer"><option value="viewer">Viewer</option><option value="reviewer">Reviewer</option><option value="editor">Editor</option><option value="operator">Operator</option><option value="admin">Admin</option></select></label>{message && <div className={`notice ${message.toLowerCase().includes("failed") ? "error" : ""}`}>{message}</div>}<button className="primary">Create account</button></form></section></div></>;
}

type OIDCConfiguration = {
  version_number: number; enabled: boolean; issuer: string; client_id: string; has_client_secret: boolean;
  authorization_endpoint: string; token_endpoint: string; jwks_uri: string; scopes: string[];
  username_claim: string; display_name_claim: string; role_claim: string;
  role_mapping: Record<string, Role>; default_role: Role | null; content_hash: string | null; comment: string;
};

function IdentityPanel({ session, onSession }: { session: Session; onSession: (value: Session) => void }) {
  const [message, setMessage] = useState("");
  const [enrollment, setEnrollment] = useState<{ secret: string; provisioning_uri: string } | null>(null);
  const [recoveryCodes, setRecoveryCodes] = useState<string[]>([]);
  const [oidc, setOidc] = useState<OIDCConfiguration | null>(null);
  const [roleMapping, setRoleMapping] = useState('{"evidence-admins":"admin","evidence-editors":"editor","evidence-reviewers":"reviewer"}');
  const refreshSession = useCallback(async () => onSession(await api<Session>("/api/v1/auth/session")), [onSession]);
  useEffect(() => {
    if (session.user.role === "admin") api<OIDCConfiguration>("/api/v1/auth/oidc/configuration").then(value => { setOidc(value); if (Object.keys(value.role_mapping).length) setRoleMapping(JSON.stringify(value.role_mapping, null, 2)); }).catch(error => setMessage(error.message));
  }, [session.user.role]);
  async function enroll(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const data = new FormData(event.currentTarget);
    try { setEnrollment(await api<{ secret: string; provisioning_uri: string }>("/api/v1/auth/totp/enroll", { method: "POST", body: JSON.stringify({ current_password: data.get("current_password") }) }, session.csrf_token)); setMessage("Scan or enter the one-time secret, then confirm a current code. The secret is shown only during enrollment."); }
    catch (caught) { setMessage(caught instanceof Error ? caught.message : "TOTP enrollment failed"); }
  }
  async function confirm(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const data = new FormData(event.currentTarget);
    try { const result = await api<{ recovery_codes: string[] }>("/api/v1/auth/totp/confirm", { method: "POST", body: JSON.stringify({ code: data.get("code") }) }, session.csrf_token); setRecoveryCodes(result.recovery_codes); setEnrollment(null); setMessage("TOTP is enabled. Store every recovery code offline; each can be used once."); await refreshSession(); }
    catch (caught) { setMessage(caught instanceof Error ? caught.message : "TOTP confirmation failed"); }
  }
  async function disable(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const data = new FormData(event.currentTarget);
    try { await api("/api/v1/auth/totp/disable", { method: "POST", body: JSON.stringify({ current_password: data.get("current_password"), code: data.get("code") }) }, session.csrf_token); setRecoveryCodes([]); setMessage("TOTP has been disabled and its secret and recovery hashes were removed."); await refreshSession(); }
    catch (caught) { setMessage(caught instanceof Error ? caught.message : "TOTP disable failed"); }
  }
  async function saveOidc(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const data = new FormData(event.currentTarget);
    try {
      const secret = String(data.get("client_secret") || "");
      const payload = {
        enabled: data.get("enabled") === "on", issuer: data.get("issuer"), client_id: data.get("client_id"), client_secret: secret || null,
        authorization_endpoint: data.get("authorization_endpoint"), token_endpoint: data.get("token_endpoint"), jwks_uri: data.get("jwks_uri"),
        scopes: String(data.get("scopes")).split(/\s+/).filter(Boolean), username_claim: data.get("username_claim"), display_name_claim: data.get("display_name_claim"),
        role_claim: data.get("role_claim"), role_mapping: JSON.parse(roleMapping), default_role: data.get("default_role") || null,
        comment: data.get("comment"),
      };
      const result = await api<OIDCConfiguration>("/api/v1/auth/oidc/configuration", { method: "POST", body: JSON.stringify(payload) }, session.csrf_token);
      setOidc(result); setMessage(`OIDC configuration v${result.version_number} is active. The client secret remains encrypted and redacted.`);
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "OIDC configuration failed"); }
  }
  return <><div className="page-heading"><div><p className="eyebrow">Identity and session security</p><h1>Authentication</h1></div><span className="chip">{session.user.identity_provider}</span></div>
    {message && <div className="notice">{message}</div>}
    <div className="split-grid"><section className="panel"><div className="panel-title"><div><p className="eyebrow">Optional second factor</p><h2>Authenticator app</h2></div><span className={`status ${session.user.totp_enabled ? "good" : "waiting"}`}>{session.user.totp_enabled ? "enabled" : "disabled"}</span></div>
      {session.user.identity_provider === "oidc" ? <p className="muted">Second-factor policy for this account is enforced by the configured identity provider.</p> : session.user.totp_enabled ? <form onSubmit={disable}><label>Current password<input name="current_password" type="password" minLength={8} autoComplete="current-password" required /></label><label>Current authenticator or recovery code<input name="code" autoComplete="one-time-code" required /></label><button className="secondary danger">Disable TOTP</button></form> : <form onSubmit={enroll}><label>Current password<input name="current_password" type="password" minLength={8} autoComplete="current-password" required /></label><button className="primary">Begin TOTP enrollment</button></form>}
      {enrollment && <div className="notice"><strong>One-time enrollment secret</strong><code>{enrollment.secret}</code><small className="muted">URI: {enrollment.provisioning_uri}</small><form onSubmit={confirm}><label>Current six-digit code<input name="code" inputMode="numeric" autoComplete="one-time-code" pattern="[0-9]{6,8}" required /></label><button className="primary">Confirm and issue recovery codes</button></form></div>}
      {recoveryCodes.length > 0 && <div className="notice"><strong>One-time recovery codes</strong><pre>{recoveryCodes.join("\n")}</pre><small>They are never returned again. The database stores only hashes.</small></div>}
    </section><section className="panel"><h2>Session controls</h2><p>Cookies are HttpOnly, SameSite strict, secure in production, and paired with a per-session CSRF token. Repeated authentication failures are durably throttled by username and client address.</p><div className="card-list"><article className="profile-card subject"><div><strong>Role</strong><small>{session.user.role}</small></div><span className="status good">API enforced</span></article><article className="profile-card subject"><div><strong>Identity source</strong><small>{session.user.identity_provider}</small></div><span className="status good">audited</span></article></div></section></div>
    {session.user.role === "admin" && <section className="panel"><div className="panel-title"><div><p className="eyebrow">Versioned federation</p><h2>OpenID Connect</h2></div><span className={`status ${oidc?.enabled ? "good" : "waiting"}`}>{oidc?.enabled ? `enabled · v${oidc.version_number}` : "disabled"}</span></div><p className="muted">Configure Authorization Code + PKCE endpoints and an explicit claim-to-role mapping. Existing secrets are retained when the secret field is blank.</p><form onSubmit={saveOidc}><label className="checkbox"><input name="enabled" type="checkbox" defaultChecked={oidc?.enabled} /> Enable OIDC login</label><div className="form-pair"><label>Issuer<input name="issuer" type="url" defaultValue={oidc?.issuer || "https://identity.example.com"} required /></label><label>Client ID<input name="client_id" defaultValue={oidc?.client_id || "evidence-studio"} required /></label></div><label>Client secret<input name="client_secret" type="password" minLength={8} placeholder={oidc?.has_client_secret ? "Leave blank to retain encrypted secret" : "Required for first version"} /></label><label>Authorization endpoint<input name="authorization_endpoint" type="url" defaultValue={oidc?.authorization_endpoint || "https://identity.example.com/oauth2/authorize"} required /></label><div className="form-pair"><label>Token endpoint<input name="token_endpoint" type="url" defaultValue={oidc?.token_endpoint || "https://identity.example.com/oauth2/token"} required /></label><label>JWKS URI<input name="jwks_uri" type="url" defaultValue={oidc?.jwks_uri || "https://identity.example.com/.well-known/jwks.json"} required /></label></div><label>Scopes<input name="scopes" defaultValue={(oidc?.scopes || ["openid", "profile", "email"]).join(" ")} required /></label><div className="form-pair"><label>Username claim<input name="username_claim" defaultValue={oidc?.username_claim || "preferred_username"} required /></label><label>Display-name claim<input name="display_name_claim" defaultValue={oidc?.display_name_claim || "name"} required /></label></div><label>Role/group claim<input name="role_claim" defaultValue={oidc?.role_claim || "groups"} required /></label><label>Explicit role mapping JSON<textarea rows={6} value={roleMapping} onChange={event => setRoleMapping(event.target.value)} spellCheck={false} /></label><label>Default role<select name="default_role" defaultValue={oidc?.default_role || ""}><option value="">No default—reject unmapped identities</option><option value="viewer">Viewer</option><option value="reviewer">Reviewer</option><option value="editor">Editor</option><option value="operator">Operator</option><option value="admin">Admin</option></select></label><label>Version comment<input name="comment" minLength={3} defaultValue="OIDC configuration reviewed and activated through the administration UI." required /></label><button className="primary">Create and activate immutable version</button></form></section>}
  </>;
}

function AuditPanel() {
  const [events, setEvents] = useState<AuditEvent[]>([]); const [message, setMessage] = useState("");
  const [visibleCount, setVisibleCount] = useState(25);
  const [query, setQuery] = useState("");
  const [actionFilter, setActionFilter] = useState("all");
  useEffect(() => { api<AuditEvent[]>("/api/v1/audit-events?limit=100").then(setEvents).catch(e => setMessage(e.message)); }, []);
  const normalizedQuery = query.trim().toLowerCase();
  const actionOptions = Array.from(new Set(events.map(event => event.action))).sort();
  const filteredEvents = events.filter(event => {
    if (actionFilter !== "all" && event.action !== actionFilter) return false;
    if (!normalizedQuery) return true;
    return [event.action, event.target_type || "", event.target_id || "", event.correlation_id].some(value => value.toLowerCase().includes(normalizedQuery));
  });
  const visibleEvents = filteredEvents.slice(0, visibleCount);
  return <><div className="page-heading"><div><p className="eyebrow">Append-only history</p><h1>Audit log</h1><p className="muted">Search by action, target or correlation ID when investigating a workflow or operator action.</p></div><span className="chip">Showing {Math.min(visibleCount, filteredEvents.length)} of {filteredEvents.length}</span></div><section className="panel"><div className="audit-toolbar"><label>Find event<input type="search" value={query} onChange={event => { setQuery(event.target.value); setVisibleCount(25); }} placeholder="Action, target ID or correlation ID" /></label><label>Action<select value={actionFilter} onChange={event => { setActionFilter(event.target.value); setVisibleCount(25); }}><option value="all">All actions</option>{actionOptions.map(action => <option key={action} value={action}>{action}</option>)}</select></label><span className="muted">{events.length} retained events loaded</span></div><div className="audit-list">{visibleEvents.map(event => <article className="audit-row" key={event.id}><time>{new Date(event.occurred_at).toLocaleString()}</time><div><strong>{event.action}</strong><p>{event.target_type ? `${event.target_type} · ${event.target_id}` : "System event"}</p></div><code title={event.correlation_id}>{event.correlation_id.slice(0, 12)}</code></article>)}{!visibleEvents.length && <p className="empty">{message || "No events match this search and action filter."}</p>}</div>{visibleCount < filteredEvents.length && <button className="secondary" onClick={() => setVisibleCount(count => Math.min(count + 25, filteredEvents.length))}>Show 25 more ({filteredEvents.length - visibleCount} remaining)</button>}</section></>;
}

type EditableProfile = { kind: "channel"; profile: ChannelProfile } | { kind: "subject"; profile: SubjectProfile };
type ArchiveProfile = EditableProfile;

function linesFromForm(data: FormData, name: string): string[] {
  return String(data.get(name) || "").split("\n").map(value => value.trim()).filter(Boolean);
}

function linesFromText(value: string): string[] {
  return value.split("\n").map(item => item.trim()).filter(Boolean);
}

function commaListFromForm(data: FormData, name: string): string[] {
  return String(data.get(name) || "").split(",").map(value => value.trim()).filter(Boolean);
}

function recordNumber(record: Record<string, unknown>, name: string, fallback: number): number {
  const value = Number(record[name]);
  return Number.isFinite(value) ? value : fallback;
}

function recordStrings(record: Record<string, unknown>, name: string): string[] {
  const value = record[name];
  return Array.isArray(value) ? value.map(item => String(item)) : [];
}

function automationWorkflow(channel: ChannelProfile): ChannelAutomationWorkflow | null {
  const value = channel.editorial_rules.automation_workflow;
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  return value as ChannelAutomationWorkflow;
}

function jsonObjectFromForm(data: FormData, name: string): Record<string, unknown> {
  const value = JSON.parse(String(data.get(name) || "{}")) as unknown;
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(`${readableDetailName(name)} must be a JSON object.`);
  return value as Record<string, unknown>;
}

const BRAND_DEFAULTS: BrandKit = {
  schema_version: 1, logo_text: "TF", tagline: "",
  primary: "#174C3C", accent: "#D6A43A", background: "#F6F3EA", surface: "#FFFFFF", text: "#18201D", muted_text: "#56615C",
  heading_font: "serif", body_font: "sans", corner_style: "soft", motion_style: "calm", image_treatment: "editorial",
  visual_style: "Calm, clear and explanatory; visuals support the narration instead of decorating it.",
};

function brandFont(value: BrandKit["heading_font"] | BrandKit["body_font"]): string {
  if (value === "serif") return "Georgia, 'Times New Roman', serif";
  if (value === "mono") return "ui-monospace, SFMono-Regular, Menlo, monospace";
  if (value === "rounded") return "'Arial Rounded MT Bold', 'Trebuchet MS', sans-serif";
  return "Inter, ui-sans-serif, system-ui, sans-serif";
}

function BrandColorField({ label, field, value, onChange }: { label: string; field: BrandKitTextField; value: string; onChange: (field: BrandKitTextField, value: string) => void }) {
  const valid = /^#[0-9A-Fa-f]{6}$/.test(value);
  return <label className="brand-color-field">{label}<span><input type="color" value={valid ? value : "#000000"} aria-label={`${label} color picker`} onChange={event => onChange(field, event.target.value.toUpperCase())} /><input value={value} pattern="#[0-9A-Fa-f]{6}" maxLength={7} aria-label={`${label} hexadecimal color`} onChange={event => onChange(field, event.target.value.toUpperCase())} required /></span></label>;
}

function BrandKitEditor({ value, channelName, onChange }: { value: BrandKit; channelName: string; onChange: (value: BrandKit) => void }) {
  const update = (field: BrandKitTextField, next: string) => onChange({ ...value, [field]: next } as BrandKit);
  const radius = value.corner_style === "square" ? 0 : value.corner_style === "rounded" ? 28 : 12;
  return <section className="brand-kit-editor" aria-labelledby="brand-kit-title">
    <div className="brand-kit-heading"><div><p className="eyebrow">Channel Brand Kit</p><h3 id="brand-kit-title">Viewer-facing identity</h3><p>Saved with the Channel version and snapshotted into every new Production Manifest.</p></div><span className="chip">WCAG AA checked</span></div>
    <div className="brand-kit-layout"><div className="brand-kit-fields">
      <fieldset><legend>Identity</legend><div className="form-pair"><label>Logo text<input value={value.logo_text} maxLength={12} onChange={event => update("logo_text", event.target.value)} required /></label><label>Tagline<input value={value.tagline} maxLength={160} onChange={event => update("tagline", event.target.value)} /></label></div></fieldset>
      <fieldset><legend>Semantic colors</legend><div className="brand-color-grid"><BrandColorField label="Primary" field="primary" value={value.primary} onChange={update} /><BrandColorField label="Accent" field="accent" value={value.accent} onChange={update} /><BrandColorField label="Background" field="background" value={value.background} onChange={update} /><BrandColorField label="Surface" field="surface" value={value.surface} onChange={update} /><BrandColorField label="Text" field="text" value={value.text} onChange={update} /><BrandColorField label="Muted text" field="muted_text" value={value.muted_text} onChange={update} /></div><small>TubeFactory rejects text/background combinations below WCAG AA contrast.</small></fieldset>
      <fieldset><legend>Typography and motion</legend><div className="form-pair"><label>Heading family<select value={value.heading_font} onChange={event => update("heading_font", event.target.value)}><option value="serif">Serif</option><option value="sans">Sans</option><option value="rounded">Rounded</option><option value="mono">Monospace</option></select></label><label>Body family<select value={value.body_font} onChange={event => update("body_font", event.target.value)}><option value="sans">Sans</option><option value="serif">Serif</option><option value="rounded">Rounded</option><option value="mono">Monospace</option></select></label><label>Corner style<select value={value.corner_style} onChange={event => update("corner_style", event.target.value)}><option value="square">Square</option><option value="soft">Soft</option><option value="rounded">Rounded</option></select></label><label>Motion style<select value={value.motion_style} onChange={event => update("motion_style", event.target.value)}><option value="still">Still</option><option value="calm">Calm</option><option value="dynamic">Dynamic</option></select></label><label>Image treatment<select value={value.image_treatment} onChange={event => update("image_treatment", event.target.value)}><option value="clean">Clean</option><option value="editorial">Editorial</option><option value="documentary">Documentary</option><option value="cinematic">Cinematic</option></select></label></div></fieldset>
      <label>Visual direction<textarea rows={4} maxLength={1000} value={value.visual_style} onChange={event => update("visual_style", event.target.value)} required /></label>
    </div><aside className="brand-preview-wrap"><span>Live 16:9 preview</span><div className="brand-preview" style={{ background: value.background, color: value.text, borderRadius: radius, fontFamily: brandFont(value.body_font) }}><div className="brand-preview-art" style={{ background: `linear-gradient(135deg, ${value.primary}, ${value.accent})` }} aria-hidden="true" /><div className="brand-preview-content"><div className="brand-preview-mark" style={{ background: value.primary, color: value.surface, borderRadius: radius / 2 }}>{value.logo_text || "—"}</div><small style={{ color: value.muted_text }}>{channelName || "Channel"}</small><h4 style={{ fontFamily: brandFont(value.heading_font) }}>A complex idea, explained one clear step at a time.</h4><p style={{ color: value.muted_text }}>{value.tagline || "Evidence, context and open questions remain visible."}</p><span style={{ color: value.primary, borderColor: value.accent }}>Source-linked explanation</span></div></div><p>The preview demonstrates hierarchy and palette. Scene Media Assets retain their own approved content and provenance.</p></aside></div>
  </section>;
}

function ChannelBrandPreview({ brand, channelName }: { brand: BrandKit; channelName: string }) {
  const kit = { ...BRAND_DEFAULTS, ...brand };
  const radius = kit.corner_style === "square" ? 0 : kit.corner_style === "rounded" ? 16 : 8;
  return <div className="channel-brand-preview" aria-label={`Brand Kit for ${channelName}`}>
    <span className="channel-brand-mark" style={{ background: kit.primary, color: kit.surface, borderRadius: radius }}>{kit.logo_text || "—"}</span>
    <span><small>Brand Kit</small><strong>{kit.tagline || kit.visual_style}</strong></span>
    <i aria-hidden="true" style={{ background: kit.accent }} />
  </div>;
}

function ProfileEditDialog({ target, channels, onClose, onSaveChannel, onSaveSubject }: {
  target: EditableProfile;
  channels: ChannelProfile[];
  onClose: () => void;
  onSaveChannel: (profile: ChannelProfile, payload: Record<string, unknown>) => Promise<void>;
  onSaveSubject: (profile: SubjectProfile, payload: Record<string, unknown>) => Promise<void>;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [brandKit, setBrandKit] = useState<BrandKit>(() => target.kind === "channel" ? { ...BRAND_DEFAULTS, ...target.profile.brand_kit } : BRAND_DEFAULTS);
  const [previewChannelName, setPreviewChannelName] = useState(target.profile.name);
  const profile = target.profile;
  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    const previouslyFocused = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    dialog.showModal();
    return () => { if (dialog.open) dialog.close(); previouslyFocused?.focus(); };
  }, []);

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setError(""); const data = new FormData(event.currentTarget);
    try {
      if (target.kind === "channel") {
        await onSaveChannel(target.profile, {
          slug: data.get("slug"), name: data.get("name"), enabled: target.profile.enabled,
          identity: { ...target.profile.identity, description: data.get("description") },
          languages: commaListFromForm(data, "languages"), audience: jsonObjectFromForm(data, "audience"),
          editorial_rules: jsonObjectFromForm(data, "editorial_rules"), brand_kit: brandKit,
          default_render_settings: jsonObjectFromForm(data, "default_render_settings"),
          default_publish_settings: jsonObjectFromForm(data, "default_publish_settings"),
        });
      } else {
        const subject = target.profile;
        await onSaveSubject(subject, {
          channel_profile_id: data.get("channel_profile_id"), name: data.get("name"), enabled: subject.enabled,
          topic: data.get("topic"), research_goal: data.get("research_goal"), excluded_angles: linesFromForm(data, "excluded_angles"),
          seed_queries: linesFromForm(data, "seed_queries"), related_concepts: linesFromForm(data, "related_concepts"),
          negative_keywords: linesFromForm(data, "negative_keywords"), languages: commaListFromForm(data, "languages"), regions: commaListFromForm(data, "regions"),
          domain_policy: { ...subject.domain_policy, allow: linesFromForm(data, "allowed_domains"), block: linesFromForm(data, "blocked_domains") },
          source_requirements: { ...subject.source_requirements, minimum_independent: Number(data.get("minimum_independent")), minimum_primary: Number(data.get("minimum_primary")) },
          schedule: { cron: String(data.get("cron") || "").trim() || null, timezone: String(data.get("timezone") || "UTC").trim() },
          freshness_policy: { ...subject.freshness_policy, lookback_days: Number(data.get("lookback_days")), maximum_source_age_days: Number(data.get("maximum_source_age_days")) },
          format_policy: { ...subject.format_policy, target: data.get("format_target"), duration_seconds: Number(data.get("duration_seconds")), minimum_duration_seconds: Number(data.get("minimum_duration_seconds")), maximum_duration_seconds: Number(data.get("maximum_duration_seconds")), speaking_rate_wpm: Number(data.get("speaking_rate_wpm")), max_enrichment_rounds: Number(data.get("max_enrichment_rounds")), minimum_coverage_units: Number(data.get("minimum_coverage_units")) },
          editorial_profile: { ...subject.editorial_profile, tone: data.get("tone") }, risk: data.get("risk"),
          budget: { ...subject.budget, tokens: Number(data.get("tokens")), gpu_seconds: Number(data.get("gpu_seconds")), currency_minor: Number(data.get("currency_minor")) },
          opportunity_weights: jsonObjectFromForm(data, "opportunity_weights"),
          approval_profile: {
            ...subject.approval_profile, mode: data.get("operating_mode"), sensitive_topics: commaListFromForm(data, "sensitive_topics"),
            evidence_density_minimum: Number(data.get("evidence_density_minimum")), repeated_scene_limit: Number(data.get("repeated_scene_limit")),
          },
        });
      }
      onClose();
    } catch (caught) { setError(caught instanceof Error ? caught.message : "Profile could not be saved"); }
    finally { setBusy(false); }
  }

  return <dialog ref={dialogRef} className={`profile-dialog ${target.kind === "channel" ? "branding-dialog" : ""}`} aria-labelledby="profile-edit-title" onCancel={event => { event.preventDefault(); if (!busy) onClose(); }}>
    <div className="profile-dialog-shell"><header className="profile-dialog-header"><div><p className="eyebrow">Versioned profile</p><h2 id="profile-edit-title">Edit {profile.name}</h2><p>Saving creates version {profile.version + 1}. Existing workflows and audit history remain linked.</p></div><button type="button" className="help-close" aria-label="Close profile editor" disabled={busy} onClick={onClose}>×</button></header>
      <form className="profile-edit-form" onSubmit={save}>{error && <div className="notice error">{error}</div>}
        {target.kind === "channel" ? <>
          <div className="form-pair"><label>Channel name<input name="name" defaultValue={target.profile.name} minLength={2} onChange={event => setPreviewChannelName(event.target.value)} required /></label><label>Slug<input name="slug" defaultValue={target.profile.slug} pattern="[a-z0-9]+(?:-[a-z0-9]+)*" required /></label></div>
          <label>Languages, comma separated<input name="languages" defaultValue={target.profile.languages.join(", ")} required /></label>
          <label>Description<textarea name="description" rows={4} defaultValue={String(target.profile.identity.description || "")} /></label>
          <BrandKitEditor value={brandKit} channelName={previewChannelName} onChange={setBrandKit} />
          <details className="profile-advanced"><summary>Advanced channel policies</summary><p className="muted">Edit valid JSON objects. Unchanged values are preserved exactly.</p><label>Audience JSON<textarea name="audience" rows={5} defaultValue={JSON.stringify(target.profile.audience, null, 2)} spellCheck={false} /></label><label>Editorial rules JSON<textarea name="editorial_rules" rows={5} defaultValue={JSON.stringify(target.profile.editorial_rules, null, 2)} spellCheck={false} /></label><label>Default render settings JSON<textarea name="default_render_settings" rows={5} defaultValue={JSON.stringify(target.profile.default_render_settings, null, 2)} spellCheck={false} /></label><label>Default publish settings JSON<textarea name="default_publish_settings" rows={5} defaultValue={JSON.stringify(target.profile.default_publish_settings, null, 2)} spellCheck={false} /></label></details>
        </> : <>
          <div className="form-pair"><label>Profile name<input name="name" defaultValue={target.profile.name} minLength={2} required /></label><label>Channel<select name="channel_profile_id" defaultValue={target.profile.channel_profile_id} required>{channels.map(channel => <option key={channel.id} value={channel.id}>{channel.name}</option>)}</select></label></div>
          <label>Topic<textarea name="topic" rows={3} defaultValue={target.profile.topic} minLength={2} required /></label><label>Research goal<textarea name="research_goal" rows={4} defaultValue={target.profile.research_goal} minLength={2} required /></label>
          <div className="form-pair"><label>Seed queries, one per line<textarea name="seed_queries" rows={5} defaultValue={target.profile.seed_queries.join("\n")} required /></label><label>Excluded terms, one per line<textarea name="negative_keywords" rows={5} defaultValue={target.profile.negative_keywords.join("\n")} /></label></div>
          <div className="form-pair"><label>Excluded angles, one per line<textarea name="excluded_angles" rows={3} defaultValue={target.profile.excluded_angles.join("\n")} /></label><label>Related concepts, one per line<textarea name="related_concepts" rows={3} defaultValue={target.profile.related_concepts.join("\n")} /></label></div>
          <div className="form-pair"><label>Languages, comma separated<input name="languages" defaultValue={target.profile.languages.join(", ")} required /></label><label>Regions, comma separated<input name="regions" defaultValue={target.profile.regions.join(", ")} /></label></div>
          <div className="form-pair"><label>Risk<select name="risk" defaultValue={target.profile.risk}><option>low</option><option>medium</option><option>high</option></select></label><label>Operating profile<select name="operating_mode" defaultValue={String(target.profile.approval_profile.mode || "assisted")}><option value="assisted">Assisted</option><option value="supervised">Supervised</option><option value="trusted">Trusted</option></select></label></div>
          <label>Sensitive categories, comma separated<input name="sensitive_topics" defaultValue={recordStrings(target.profile.approval_profile, "sensitive_topics").join(", ")} /></label>
          <div className="form-pair"><label>Minimum factual claims / 100 words<input name="evidence_density_minimum" type="number" min="0" max="100" step="0.1" defaultValue={recordNumber(target.profile.approval_profile, "evidence_density_minimum", 1)} /></label><label>Repeated-scene limit<input name="repeated_scene_limit" type="number" min="0" max="100" defaultValue={recordNumber(target.profile.approval_profile, "repeated_scene_limit", 1)} /></label></div>
          <div className="form-pair"><label>Temporal cron<input name="cron" defaultValue={target.profile.schedule.cron || ""} placeholder="0 6 * * *" /></label><label>IANA timezone<input name="timezone" defaultValue={target.profile.schedule.timezone || "UTC"} required /></label></div>
          <details className="profile-advanced"><summary>Advanced research policies</summary><div className="form-pair"><label>Allowed domains, one per line<textarea name="allowed_domains" rows={3} defaultValue={recordStrings(target.profile.domain_policy, "allow").join("\n")} /></label><label>Blocked domains, one per line<textarea name="blocked_domains" rows={3} defaultValue={recordStrings(target.profile.domain_policy, "block").join("\n")} /></label></div><div className="form-pair"><label>Minimum independent sources<input name="minimum_independent" type="number" min="0" defaultValue={recordNumber(target.profile.source_requirements, "minimum_independent", 2)} /></label><label>Minimum primary sources<input name="minimum_primary" type="number" min="0" defaultValue={recordNumber(target.profile.source_requirements, "minimum_primary", 1)} /></label></div><div className="form-pair"><label>Recent article window (days)<input name="lookback_days" type="number" min="1" max="365" defaultValue={recordNumber(target.profile.freshness_policy, "lookback_days", 7)} /></label><label>Maximum source age (days)<input name="maximum_source_age_days" type="number" min="1" defaultValue={recordNumber(target.profile.freshness_policy, "maximum_source_age_days", 3650)} /></label></div><div className="form-pair"><label>Format target<input name="format_target" defaultValue={String(target.profile.format_policy.target || "standard")} /></label><label>Target duration (seconds)<input name="duration_seconds" type="number" min="60" max="3600" defaultValue={recordNumber(target.profile.format_policy, "duration_seconds", 600)} /></label></div><div className="form-pair three"><label>Minimum duration<input name="minimum_duration_seconds" type="number" min="30" max="3600" defaultValue={recordNumber(target.profile.format_policy, "minimum_duration_seconds", 450)} /></label><label>Maximum duration<input name="maximum_duration_seconds" type="number" min="60" max="3600" defaultValue={recordNumber(target.profile.format_policy, "maximum_duration_seconds", 750)} /></label><label>Speaking rate (words/min)<input name="speaking_rate_wpm" type="number" min="80" max="220" defaultValue={recordNumber(target.profile.format_policy, "speaking_rate_wpm", 135)} /></label></div><div className="form-pair"><label>Maximum enrichment rounds<input name="max_enrichment_rounds" type="number" min="0" max="10" defaultValue={recordNumber(target.profile.format_policy, "max_enrichment_rounds", 3)} /></label><label>Minimum explanation units<input name="minimum_coverage_units" type="number" min="3" max="20" defaultValue={recordNumber(target.profile.format_policy, "minimum_coverage_units", 6)} /></label></div><label>Editorial tone<input name="tone" defaultValue={String(target.profile.editorial_profile.tone || "calm")} /></label><div className="form-pair three"><label>Token budget<input name="tokens" type="number" min="0" defaultValue={recordNumber(target.profile.budget, "tokens", 0)} /></label><label>GPU seconds<input name="gpu_seconds" type="number" min="0" defaultValue={recordNumber(target.profile.budget, "gpu_seconds", 0)} /></label><label>Currency minor units<input name="currency_minor" type="number" min="0" defaultValue={recordNumber(target.profile.budget, "currency_minor", 0)} /></label></div><label>Opportunity weights JSON<textarea name="opportunity_weights" rows={5} defaultValue={JSON.stringify(target.profile.opportunity_weights, null, 2)} spellCheck={false} /></label></details>
        </>}
        <footer className="profile-dialog-actions"><button type="button" className="secondary" disabled={busy} onClick={onClose}>Cancel</button><button className="primary" disabled={busy}>{busy ? "Saving…" : "Save new version"}</button></footer>
      </form>
    </div>
  </dialog>;
}

function ArchiveProfileDialog({ target, blockingSubjects, onClose, onArchive }: { target: ArchiveProfile; blockingSubjects: SubjectProfile[]; onClose: () => void; onArchive: (target: ArchiveProfile) => Promise<void> }) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    const previouslyFocused = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    dialog.showModal();
    return () => { if (dialog.open) dialog.close(); previouslyFocused?.focus(); };
  }, []);
  async function archive() {
    setBusy(true); setError("");
    try { await onArchive(target); onClose(); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "Profile could not be archived"); }
    finally { setBusy(false); }
  }
  const blocked = target.kind === "channel" && blockingSubjects.length > 0;
  return <dialog ref={dialogRef} className="archive-dialog" aria-labelledby="profile-archive-title" onCancel={event => { event.preventDefault(); if (!busy) onClose(); }}><div className="archive-dialog-shell"><p className="eyebrow">Recoverable removal</p><h2 id="profile-archive-title">Archive {target.profile.name}?</h2>{target.kind === "subject" ? <p>The subject disappears from active lists and its Temporal schedule is paused first. Findings, workflows and audit history remain intact.</p> : blocked ? <><div className="notice error">Archive or reassign these subjects first:</div><ul>{blockingSubjects.map(subject => <li key={subject.id}>{subject.name}</li>)}</ul></> : <p>The channel disappears from active lists. Its workflows, evidence and audit history remain intact.</p>}{error && <div className="notice error">{error}</div>}<div className="profile-dialog-actions"><button type="button" className="secondary" disabled={busy} onClick={onClose}>Cancel</button><button type="button" className="secondary danger" disabled={busy || blocked} onClick={archive}>{busy ? "Archiving…" : `Archive ${target.kind}`}</button></div></div></dialog>;
}

function ProfilesPanel({ csrf, role, activeChannelId, onChannelsChanged }: { csrf: string; role: Role; activeChannelId: string; onChannelsChanged: () => Promise<void> }) {
  const [channels, setChannels] = useState<ChannelProfile[]>([]);
  const [subjects, setSubjects] = useState<SubjectProfile[]>([]);
  const [selectedChannel, setSelectedChannel] = useState("");
  const [plan, setPlan] = useState<SearchPlan | null>(null);
  const [editingProfile, setEditingProfile] = useState<EditableProfile | null>(null);
  const [archiveProfile, setArchiveProfile] = useState<ArchiveProfile | null>(null);
  const [scheduleStates, setScheduleStates] = useState<Record<string, SubjectSchedule>>({});
  const [message, setMessage] = useState("");
  const refresh = useCallback(async () => {
    try {
      const [nextChannels, nextSubjects] = await Promise.all([
        api<ChannelProfile[]>("/api/v1/channel-profiles"),
        api<SubjectProfile[]>("/api/v1/subject-profiles"),
      ]);
      setChannels(nextChannels); setSubjects(nextSubjects);
      setSelectedChannel(current => activeChannelId || current || nextChannels[0]?.id || "");
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Profiles could not be loaded"); }
  }, [activeChannelId]);
  useEffect(() => { refresh(); }, [refresh]);

  async function createChannel(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setMessage(""); const form = event.currentTarget; const data = new FormData(form);
    try {
      await api("/api/v1/channel-profiles", { method: "POST", body: JSON.stringify({
        slug: data.get("slug"), name: data.get("name"), enabled: false,
        identity: { description: data.get("description") }, languages: String(data.get("languages") || "en").split(",").map(value => value.trim()),
        audience: {}, editorial_rules: { evidence_first: true }, brand_kit: {}, default_render_settings: {},
        default_publish_settings: { privacy: "private", automatic_publication: false },
      }) }, csrf);
      form.reset(); setMessage("Disabled channel profile created. Open its Brand Kit before enabling if this Channel needs a distinct viewer-facing style."); await Promise.all([refresh(), onChannelsChanged()]);
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Channel creation failed"); }
  }

  async function createSubject(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setMessage(""); const form = event.currentTarget; const data = new FormData(form);
    try {
      await api("/api/v1/subject-profiles", { method: "POST", body: JSON.stringify({
        channel_profile_id: data.get("channel_profile_id"), name: data.get("name"), enabled: false,
        topic: data.get("topic"), research_goal: data.get("research_goal"), excluded_angles: [],
        seed_queries: String(data.get("seed_queries") || "").split("\n").map(value => value.trim()).filter(Boolean),
        related_concepts: [], negative_keywords: String(data.get("negative_keywords") || "").split("\n").map(value => value.trim()).filter(Boolean), languages: [data.get("language") || "en"], regions: [],
        domain_policy: { allow: [], block: [] }, source_requirements: { minimum_independent: 2, minimum_primary: 1, expected_primary_types: ["official record", "original study"] },
        schedule: { cron: String(data.get("cron") || "").trim() || null, timezone: String(data.get("timezone") || "UTC").trim() }, freshness_policy: { lookback_days: Number(data.get("lookback_days") || 7), maximum_source_age_days: 3650 },
        format_policy: { target: "standard", duration_seconds: 480, minimum_duration_seconds: 360, maximum_duration_seconds: 600, speaking_rate_wpm: 135, max_enrichment_rounds: 3, minimum_coverage_units: 6 }, editorial_profile: { tone: "calm" }, risk: data.get("risk"),
        budget: { tokens: 0, gpu_seconds: 0, currency_minor: 0 }, opportunity_weights: {}, approval_profile: {
          mode: data.get("operating_mode"),
          sensitive_topics: String(data.get("sensitive_topics") || "").split(",").map(value => value.trim()).filter(Boolean),
          evidence_density_minimum: Number(data.get("evidence_density_minimum")),
          repeated_scene_limit: Number(data.get("repeated_scene_limit")),
        },
      }) }, csrf);
      form.reset(); setMessage("Disabled subject profile created. Test its search plan before scheduling."); await refresh();
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Subject creation failed"); }
  }

  async function testPlan(id: string) {
    try { setPlan(await api<SearchPlan>(`/api/v1/subject-profiles/${id}/test-search-plan`, { method: "POST" }, csrf)); }
    catch (caught) { setMessage(caught instanceof Error ? caught.message : "Search-plan test failed"); }
  }

  async function setChannelEnabled(channel: ChannelProfile, enabled: boolean) {
    const { id: _id, version, created_at: _created, updated_at: _updated, ...writeFields } = channel;
    try {
      await api(`/api/v1/channel-profiles/${channel.id}`, { method: "PUT", body: JSON.stringify({ ...writeFields, enabled, expected_version: version }) }, csrf);
      setMessage(`Channel ${enabled ? "enabled" : "disabled"}.`); await Promise.all([refresh(), onChannelsChanged()]);
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Channel update failed"); }
  }

  async function setSubjectEnabled(subject: SubjectProfile, enabled: boolean) {
    const { id: _id, version, created_at: _created, updated_at: _updated, ...writeFields } = subject;
    try {
      await api(`/api/v1/subject-profiles/${subject.id}`, { method: "PUT", body: JSON.stringify({ ...writeFields, enabled, expected_version: version }) }, csrf);
      setMessage(`Subject ${enabled ? "enabled" : "disabled"}. Reconcile its Temporal schedule to apply the change.`); await refresh();
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Subject update failed"); }
  }

  async function updateChannel(channel: ChannelProfile, payload: Record<string, unknown>) {
    await api(`/api/v1/channel-profiles/${channel.id}`, { method: "PUT", body: JSON.stringify({ ...payload, expected_version: channel.version }) }, csrf);
    setMessage(`Channel saved as version ${channel.version + 1}.`); await Promise.all([refresh(), onChannelsChanged()]);
  }

  async function updateSubject(subject: SubjectProfile, payload: Record<string, unknown>) {
    await api(`/api/v1/subject-profiles/${subject.id}`, { method: "PUT", body: JSON.stringify({ ...payload, expected_version: subject.version }) }, csrf);
    setMessage(`Subject saved as version ${subject.version + 1}. Apply its schedule if the cron, timezone or enabled state changed.`); await refresh();
  }

  async function archiveSelected(target: ArchiveProfile) {
    await api(`/api/v1/${target.kind === "channel" ? "channel" : "subject"}-profiles/${target.profile.id}?expected_version=${target.profile.version}`, { method: "DELETE" }, csrf);
    setMessage(`${target.kind === "channel" ? "Channel" : "Subject"} archived. Linked history remains available in audit and workflow records.`);
    if (target.kind === "channel") await Promise.all([refresh(), onChannelsChanged()]);
    else await refresh();
  }

  async function reconcileSchedule(subject: SubjectProfile) {
    try {
      const state = await api<SubjectSchedule>(`/api/v1/subject-profiles/${subject.id}/schedule/reconcile`, { method: "POST" }, csrf);
      setScheduleStates(current => ({ ...current, [subject.id]: state }));
      setMessage(state.exists ? `Schedule ${state.paused ? "paused" : "active"}; ${state.next_action_times.length ? `next ${new Date(state.next_action_times[0]).toLocaleString()}` : "no next action"}.` : "No cron is configured; no Temporal schedule exists.");
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Schedule reconciliation failed"); }
  }

  const scopedSubjects = activeChannelId ? subjects.filter(subject => subject.channel_profile_id === activeChannelId) : subjects;
  const channelNames = new Map(channels.map(channel => [channel.id, channel.name]));
  const automatedChannels = channels.map(channel => ({ channel, workflow: automationWorkflow(channel) }))
    .filter(item => item.workflow && (!activeChannelId || item.channel.id === activeChannelId)) as { channel: ChannelProfile; workflow: ChannelAutomationWorkflow }[];
  return <><div className="page-heading"><div><p className="eyebrow">Discovery configuration</p><h1>Channels and subjects</h1></div><span className="status good">Monitoring online</span></div>
    {message && <div className="notice">{message}</div>}
    <div className="profile-grid">
      <section className="panel"><div className="panel-title"><div><p className="eyebrow">Editorial identities</p><h2>Channel profiles</h2></div><span className="chip">{channels.length}</span></div>
        <div className="card-list">{channels.map(channel => <article className="profile-card" key={channel.id}><div><strong>{channel.name}</strong><small>{channel.languages.join(" · ")} · v{channel.version}</small><ChannelBrandPreview brand={channel.brand_kit} channelName={channel.name} /></div><div className="actions"><span className={`status ${channel.enabled ? "good" : ""}`}>{channel.enabled ? "Enabled" : "Disabled"}</span>{(role === "admin" || role === "editor") && <><button className="secondary compact control-help" data-usage="Open the versioned Channel Brand Kit: logo text, tagline, semantic colors, typography, motion, image treatment and visual direction." onClick={() => setEditingProfile({ kind: "channel", profile: channel })}>Brand Kit</button><button className="secondary compact control-help" data-usage="Edit the complete versioned channel profile, including advanced policies, without changing linked history." onClick={() => setEditingProfile({ kind: "channel", profile: channel })}>Edit</button><button className="secondary compact" onClick={() => setChannelEnabled(channel, !channel.enabled)}>{channel.enabled ? "Disable" : "Enable"}</button><button className="secondary compact danger control-help" data-usage="Archive this channel after its subjects are archived or moved. History is retained." onClick={() => setArchiveProfile({ kind: "channel", profile: channel })}>Archive</button></>}</div></article>)}{!channels.length && <p className="empty">Create a disabled editorial identity to begin.</p>}</div>
        {(role === "admin" || role === "editor") && <details className="inline-create"><summary><span>Create channel</span><strong>Add a disabled channel profile</strong><small>Use this only when you need a new editorial identity.</small></summary><form onSubmit={createChannel}><label>Channel name<input name="name" required /></label><label>Slug<input name="slug" pattern="[a-z0-9]+(?:-[a-z0-9]+)*" required /></label><label>Languages<input name="languages" defaultValue="en" required /></label><label>Description<textarea name="description" rows={3} /></label><button className="primary">Create disabled channel</button></form></details>}
      </section>
      <section className="panel"><div className="panel-title"><div><p className="eyebrow">Scheduled monitoring</p><h2>Subject profiles</h2></div><span className="chip">{scopedSubjects.length}</span></div>
        <div className="card-list">{scopedSubjects.map(subject => <article className="profile-card subject" key={subject.id}><div><strong>{subject.name}</strong><small>{channelNames.get(subject.channel_profile_id) || "Unknown channel"} · {subject.risk} risk · {subject.seed_queries.length} seed quer{subject.seed_queries.length === 1 ? "y" : "ies"}</small><small>{subject.schedule.cron || "Manual only"}{scheduleStates[subject.id] ? ` · ${scheduleStates[subject.id].exists ? scheduleStates[subject.id].paused ? "schedule paused" : "schedule active" : "schedule absent"}` : ""}</small></div><div className="actions">{(role === "admin" || role === "editor") && <><button className="secondary compact control-help" data-usage="Edit this subject's queries, policies, channel and schedule as a new version." onClick={() => setEditingProfile({ kind: "subject", profile: subject })}>Edit</button><button className="secondary compact" onClick={() => testPlan(subject.id)}>Test plan</button><button className="secondary compact" onClick={() => setSubjectEnabled(subject, !subject.enabled)}>{subject.enabled ? "Disable" : "Enable"}</button><button className="secondary compact danger control-help" data-usage="Pause the schedule and archive this subject while retaining findings and audit history." onClick={() => setArchiveProfile({ kind: "subject", profile: subject })}>Archive</button></>}{(role === "admin" || role === "operator") && <button className="secondary compact" onClick={() => reconcileSchedule(subject)}>Apply schedule</button>}</div></article>)}{!scopedSubjects.length && <p className="empty">No subjects belong to this channel yet.</p>}</div>
        {(role === "admin" || role === "editor") && <details className="inline-create"><summary><span>Create subject</span><strong>Add a disabled monitoring profile</strong><small>Existing subjects remain above; enable and schedule after review.</small></summary><form onSubmit={createSubject}><label>Channel<select name="channel_profile_id" value={selectedChannel} onChange={event => setSelectedChannel(event.target.value)} required><option value="">Select a channel</option>{channels.map(channel => <option key={channel.id} value={channel.id}>{channel.name}</option>)}</select></label><label>Profile name<input name="name" required /></label><label>Topic<input name="topic" required /></label><label>Research goal<textarea name="research_goal" rows={3} required /></label><label>Seed queries, one per line<textarea name="seed_queries" rows={3} required /></label><label>Excluded terms, one per line<textarea name="negative_keywords" rows={2} placeholder="Satire&#10;Kommentar" /></label><div className="form-pair"><label>Language<input name="language" defaultValue="en" required /></label><label>Risk<select name="risk" defaultValue="medium"><option>low</option><option>medium</option><option>high</option></select></label></div><div className="form-pair"><label>Operating profile<select name="operating_mode" defaultValue="assisted"><option value="assisted">Assisted</option><option value="supervised">Supervised</option><option value="trusted">Trusted</option></select></label><label>Sensitive categories, comma separated<input name="sensitive_topics" placeholder="health, politics" /></label></div><div className="form-pair"><label>Minimum factual claims / 100 words<input name="evidence_density_minimum" type="number" min="0" max="100" step="0.1" defaultValue="1" /></label><label>Repeated-scene limit<input name="repeated_scene_limit" type="number" min="0" max="100" defaultValue="1" /></label></div><small className="muted">Sensitive categories: identifiable_accusation, health, legal, finance, politics, breaking_news, misinformation_fact_checking.</small><div className="form-pair"><label>Recent article window (days)<input name="lookback_days" type="number" min="1" max="365" defaultValue="7" required /></label><label>IANA timezone<input name="timezone" defaultValue="UTC" required /></label></div><label>Temporal cron<input name="cron" placeholder="0 6 * * *" /></label><button className="primary" disabled={!channels.length}>Create disabled subject</button></form></details>}
      </section>
    </div>
    {automatedChannels.map(({ channel, workflow }) => <section className="panel channel-automation" key={`automation-${channel.id}`}><div className="panel-title"><div><p className="eyebrow">Channel-scoped production</p><h2>{workflow.name}</h2><p>{workflow.summary}</p></div><span className={`status ${workflow.enabled ? "good" : "waiting"}`}>{workflow.enabled ? `active · v${workflow.version}` : `disabled · v${workflow.version}`}</span></div><div className="automation-stage-list">{workflow.stages.map((stage, index) => <article key={stage.key}><span>{index + 1}</span><div><strong>{stage.label}</strong><small>{stage.mode === "human_gate" ? "Human approval" : stage.mode === "automatic" ? "Automatic" : "Operator assisted"}</small></div></article>)}</div><div className="automation-prompt-grid"><details><summary>Writer prompt</summary><pre>{workflow.prompts.script_writer}</pre></details><details><summary>Independent verifier prompt</summary><pre>{workflow.prompts.script_verifier}</pre></details><details><summary>Storyboard prompt</summary><pre>{workflow.prompts.storyboard}</pre></details></div><div className="notice"><strong>{workflow.research_review === "automatic_source_brief" ? "Source brief continues automatically:" : "Mandatory gates remain:"}</strong> {workflow.research_review === "automatic_source_brief" ? "After human topic selection, trusted research is retained and audited without a separate dossier action. Human approval remains required for script, storyboard, exact media render and publication." : "Opportunity, dossier, script, storyboard, exact media render and publication approval. Approval starts only the permitted next stage and never bypasses the following gate."}</div></section>)}
    {editingProfile && <ProfileEditDialog target={editingProfile} channels={channels} onClose={() => setEditingProfile(null)} onSaveChannel={updateChannel} onSaveSubject={updateSubject} />}
    {archiveProfile && <ArchiveProfileDialog target={archiveProfile} blockingSubjects={archiveProfile.kind === "channel" ? subjects.filter(subject => subject.channel_profile_id === archiveProfile.profile.id) : []} onClose={() => setArchiveProfile(null)} onArchive={archiveSelected} />}
    {plan && <section className="panel"><div className="panel-title"><div><p className="eyebrow">Validated search plan</p><h2>{plan.subject_topic}</h2></div><button className="secondary compact" onClick={() => setPlan(null)}>Close</button></div><div className="strategy-list">{plan.strategies.map((strategy, index) => <article key={`${strategy.purpose}-${index}`}><span className="chip">{strategy.purpose}</span><code>{strategy.query}</code><small>{strategy.language}{strategy.region ? ` · ${strategy.region}` : ""}</small></article>)}</div><div className="notice"><strong>Falsification branch:</strong> {plan.falsification_queries.join("; ")}</div></section>}
  </>;
}

function ArchivedProfilesPanel({ csrf, role, onChannelsChanged }: { csrf: string; role: Role; onChannelsChanged: () => Promise<void> }) {
  const [archivedChannels, setArchivedChannels] = useState<ArchivedChannelProfile[]>([]);
  const [archivedSubjects, setArchivedSubjects] = useState<ArchivedSubjectProfile[]>([]);
  const [archivedFindings, setArchivedFindings] = useState<ArchivedOpportunity[]>([]);
  const [activeChannels, setActiveChannels] = useState<ChannelProfile[]>([]);
  const [activeSubjects, setActiveSubjects] = useState<SubjectProfile[]>([]);
  const [query, setQuery] = useState("");
  const [kind, setKind] = useState<"all" | "channel" | "subject" | "finding">("all");
  const [archiveVisibleCount, setArchiveVisibleCount] = useState(25);
  const [busyId, setBusyId] = useState("");
  const [message, setMessage] = useState("");
  const canRestore = role === "admin" || role === "editor";
  const refresh = useCallback(async () => {
    try {
      const [channels, subjects, findings, active, currentSubjects] = await Promise.all([
        api<ArchivedChannelProfile[]>("/api/v1/channel-profiles/archived"),
        api<ArchivedSubjectProfile[]>("/api/v1/subject-profiles/archived"),
        api<ArchivedOpportunity[]>("/api/v1/research/opportunities/archived"),
        api<ChannelProfile[]>("/api/v1/channel-profiles"),
        api<SubjectProfile[]>("/api/v1/subject-profiles"),
      ]);
      setArchivedChannels(channels); setArchivedSubjects(subjects); setArchivedFindings(findings); setActiveChannels(active); setActiveSubjects(currentSubjects);
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Archive could not be loaded"); }
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => { setArchiveVisibleCount(25); }, [query, kind]);
  const activeChannelNames = new Map(activeChannels.map(channel => [channel.id, channel.name]));
  const archivedChannelNames = new Map(archivedChannels.map(channel => [channel.id, channel.name]));
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const subjectNames = new Map([...activeSubjects, ...archivedSubjects].map(subject => [subject.id, subject.name]));
  const subjectChannelIds = new Map([...activeSubjects, ...archivedSubjects].map(subject => [subject.id, subject.channel_profile_id]));
  const profileRecords: Array<{ kind: "channel" | "subject"; profile: ArchivedChannelProfile | ArchivedSubjectProfile; search: string }> = [
    ...archivedChannels.map(profile => ({ kind: "channel" as const, profile, search: `${profile.name} ${profile.slug} ${profile.languages.join(" ")} ${String(profile.identity.description || "")}` })),
    ...archivedSubjects.map(profile => ({ kind: "subject" as const, profile, search: `${profile.name} ${profile.topic} ${profile.research_goal} ${activeChannelNames.get(profile.channel_profile_id) || archivedChannelNames.get(profile.channel_profile_id) || ""}` })),
  ].filter(record => (kind === "all" || record.kind === kind) && (!normalizedQuery || record.search.toLocaleLowerCase().includes(normalizedQuery)))
    .sort((left, right) => new Date(right.profile.archived_at).getTime() - new Date(left.profile.archived_at).getTime());
  const findingRecords = archivedFindings.filter(finding => {
    if (kind !== "all" && kind !== "finding") return false;
    const subjectName = subjectNames.get(finding.subject_profile_id) || "Unavailable subject";
    const channelId = subjectChannelIds.get(finding.subject_profile_id) || "";
    const channelName = activeChannelNames.get(channelId) || archivedChannelNames.get(channelId) || "Unavailable Channel";
    return !normalizedQuery || `${finding.title} ${finding.summary} ${finding.decision} ${finding.score ?? ""} ${subjectName} ${channelName}`.toLocaleLowerCase().includes(normalizedQuery);
  });
  const visibleProfileRecords = profileRecords.slice(0, archiveVisibleCount);
  const remainingArchiveSlots = Math.max(0, archiveVisibleCount - visibleProfileRecords.length);
  const visibleFindingRecords = findingRecords.slice(0, remainingArchiveSlots);
  const archiveMatchCount = profileRecords.length + findingRecords.length;
  const archiveShownCount = visibleProfileRecords.length + visibleFindingRecords.length;

  async function restore(record: { kind: "channel" | "subject"; profile: ArchivedChannelProfile | ArchivedSubjectProfile }) {
    setBusyId(record.profile.id); setMessage("");
    try {
      await api(`/api/v1/${record.kind === "channel" ? "channel" : "subject"}-profiles/${record.profile.id}/restore?expected_version=${record.profile.version}`, { method: "POST" }, csrf);
      setMessage(`${record.kind === "channel" ? "Channel" : "Subject"} restored as disabled. Review it under Channels & subjects before enabling.`);
      await Promise.all([refresh(), onChannelsChanged()]);
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Profile could not be restored"); }
    finally { setBusyId(""); }
  }

  return <><div className="page-heading"><div><p className="eyebrow">Recoverable history</p><h1>Archive</h1><p>Archived Channels, subjects and scored findings are kept out of active worklists and remain inspectable here.</p></div><span className="chip">{archivedChannels.length + archivedSubjects.length + archivedFindings.length} archived</span></div>
    {message && <div className="notice">{message}</div>}
    <section className="panel archived-profile-panel">
      <div className="archive-toolbar"><label>Search archive<input type="search" value={query} onChange={event => setQuery(event.target.value)} placeholder="Name, title, topic, score or Channel" /></label><label>Record type<select value={kind} onChange={event => setKind(event.target.value as typeof kind)}><option value="all">All records</option><option value="channel">Channels</option><option value="subject">Subjects</option><option value="finding">Scored findings</option></select></label><button type="button" className="secondary" onClick={() => void refresh()}>Refresh</button></div>
      <div className="archive-summary"><span><strong>{archivedChannels.length}</strong> Channels</span><span><strong>{archivedSubjects.length}</strong> subjects</span><span><strong>{archivedFindings.length}</strong> scored findings</span><span><strong>{archiveShownCount}</strong> shown of {archiveMatchCount} matching</span><span>Profile restores are disabled by default</span></div>
      <div className="archive-record-list">{visibleProfileRecords.map(record => {
        const profile = record.profile;
        const subject = record.kind === "subject" ? profile as ArchivedSubjectProfile : null;
        const parentName = subject ? activeChannelNames.get(subject.channel_profile_id) || archivedChannelNames.get(subject.channel_profile_id) || "Unavailable Channel" : "";
        const parentActive = subject ? activeChannelNames.has(subject.channel_profile_id) : true;
        return <article className="archive-record" key={`${record.kind}-${profile.id}`}><div className="archive-record-heading"><div><span className="chip">{record.kind}</span><strong>{profile.name}</strong><small>Archived {new Date(profile.archived_at).toLocaleString()} · version {profile.version}</small>{subject && <small>Channel: {parentName} · {subject.schedule.cron || "Manual only"}</small>}</div>{canRestore && <button type="button" className="primary compact" disabled={busyId === profile.id || !parentActive} onClick={() => void restore(record)}>{busyId === profile.id ? "Restoring…" : parentActive ? `Restore ${record.kind}` : "Restore Channel first"}</button>}</div><details><summary>Inspect archived configuration</summary>{record.kind === "channel" ? <div className="archive-inspection"><p><strong>Slug:</strong> {(profile as ArchivedChannelProfile).slug}</p><p><strong>Languages:</strong> {(profile as ArchivedChannelProfile).languages.join(", ")}</p><p><strong>Description:</strong> {String((profile as ArchivedChannelProfile).identity.description || "No description")}</p></div> : <div className="archive-inspection"><p><strong>Topic:</strong> {subject?.topic}</p><p><strong>Research goal:</strong> {subject?.research_goal}</p><p><strong>Seed queries:</strong> {subject?.seed_queries.join(" · ")}</p><p><strong>Schedule:</strong> {subject?.schedule.cron || "Manual only"} · {subject?.schedule.timezone}</p></div>}<pre>{JSON.stringify(profile, null, 2)}</pre></details></article>;
      })}{visibleFindingRecords.map(finding => {
        const subjectName = subjectNames.get(finding.subject_profile_id) || "Unavailable subject";
        const channelId = subjectChannelIds.get(finding.subject_profile_id) || "";
        const channelName = activeChannelNames.get(channelId) || archivedChannelNames.get(channelId) || "Unavailable Channel";
        return <article className="archive-record" key={`finding-${finding.id}`}><div className="archive-record-heading"><div><span className="chip">scored finding</span><strong>{finding.title}</strong><small>Archived {new Date(finding.archived_at).toLocaleString()} · score {finding.score ?? "—"} · {finding.decision}</small><small>{channelName} · {subjectName} · {finding.source_count} source{finding.source_count === 1 ? "" : "s"}</small></div></div><p>{finding.summary}</p><details><summary>Inspect score trace and retained metadata</summary><div className="archive-inspection"><p><strong>Score version:</strong> {finding.score_version ?? "not scored"}</p><p><strong>Components:</strong> {Object.entries(finding.score_components).map(([name, value]) => `${name} ${value}`).join(" · ") || "None"}</p><p><strong>Penalties:</strong> {Object.entries(finding.score_penalties).map(([name, value]) => `${name} ${value}`).join(" · ") || "None"}</p><p><strong>Reasoning:</strong> {finding.score_reasoning.join(" · ") || "None"}</p></div></details></article>;
      })}{!profileRecords.length && !findingRecords.length && <p className="empty">{normalizedQuery ? "No archived record matches this search." : "No archived records in this category."}</p>}</div>
      {archiveShownCount < archiveMatchCount && <button type="button" className="secondary" onClick={() => setArchiveVisibleCount(count => Math.min(count + 25, archiveMatchCount))}>Show 25 more ({archiveMatchCount - archiveShownCount} remaining)</button>}
    </section>
  </>;
}

function ReadinessSummary({ dossier, detailed = false }: { dossier: Dossier; detailed?: boolean }) {
  const readiness = dossier.completion_evaluation.explanation_readiness;
  if (!readiness) return <div className="notice error"><strong>Explanation readiness unavailable</strong><p>This older dossier must be rerun through evidence enrichment before script generation.</p></div>;
  const wordRange = readiness.target_word_range || [];
  return <div className={`readiness-card ${readiness.ready ? "ready" : "blocked"}`}><div className="panel-title"><div><p className="eyebrow">Explanation readiness · enrichment round {readiness.enrichment_round ?? 0}</p><h3>{readiness.ready ? "Ready for a full explanation" : "Evidence enrichment required"}</h3></div><span className={`status ${readiness.ready ? "good" : "waiting"}`}>{readiness.usable_claim_count ?? 0}/{readiness.minimum_factual_claims ?? 0} usable claims</span></div><div className="score-pills"><span>{readiness.minimum_duration_seconds ?? "?"}–{readiness.maximum_duration_seconds ?? "?"} seconds</span><span>{wordRange.length === 2 ? `${wordRange[0]}–${wordRange[1]} words` : "word target pending"}</span><span>{readiness.coverage_units?.length ?? 0}/{readiness.minimum_coverage_units ?? 0} explanation units</span><span>{readiness.independent_source_count ?? 0} independent sources</span><span>{readiness.primary_source_count ?? 0} primary sources</span><span>{readiness.counterevidence_search_completed ? "counterevidence searched" : "counterevidence search missing"}</span></div>{readiness.gaps?.length ? <div className="notice error"><strong>Blocking gaps</strong><p>{readiness.gaps.join(" · ")}</p></div> : null}{detailed && readiness.coverage_units?.length ? <div className="coverage-grid">{readiness.coverage_units.map((unit, index) => <article key={unit.id || index}><span className="chip">{unit.role || "explanation step"}</span><strong>{unit.label || unit.question || unit.id || `Unit ${index + 1}`}</strong></article>)}</div> : null}</div>;
}

function ResearchPanel({ csrf, role, activeChannelId }: { csrf: string; role: Role; activeChannelId: string }) {
  const [opportunities, setOpportunities] = useState<Opportunity[]>([]);
  const [visibleOpportunityCount, setVisibleOpportunityCount] = useState(20);
  const [selectedOpportunity, setSelectedOpportunity] = useState<Opportunity | null>(null);
  const [dossiers, setDossiers] = useState<Dossier[]>([]);
  const [detail, setDetail] = useState<DossierDetail | null>(null);
  const [subjects, setSubjects] = useState<SubjectProfile[]>([]);
  const [channelSubjectIds, setChannelSubjectIds] = useState<string[]>([]);
  const [selectedSubject, setSelectedSubject] = useState("");
  const [run, setRun] = useState<ResearchWorkflowView | null>(null);
  const [reviewComment, setReviewComment] = useState("Reviewed against the cited evidence and completion rules.");
  const [overrideReason, setOverrideReason] = useState("Reviewer accepts the documented evidence limitations for this editorial decision.");
  const [dossierReviewPending, setDossierReviewPending] = useState<"approved" | "rejected" | null>(null);
  const [opportunityReason, setOpportunityReason] = useState("Editorially reviewed for evidence potential, scope, and expected cost.");
  const [opportunityRationale, setOpportunityRationale] = useState("This video deserves to exist because it answers a documented audience question with evidence and counterevidence not covered by the existing channel catalogue.");
  const [workflowReason, setWorkflowReason] = useState("Operator requested this workflow control action after reviewing its current state.");
  const [workflowLogs, setWorkflowLogs] = useState<Array<{ at: string; message: string; level?: "info" | "warning" | "error" }>>([]);
  const [message, setMessage] = useState("");
  type ResearchWorkflowView = {
    workflow_id: string;
    state: string;
    progress: number;
    result: Record<string, unknown> | null;
    execution_status: "RUNNING" | "COMPLETED" | "FAILED" | "CANCELLED" | "TERMINATED" | "TIMED_OUT";
    retryable: boolean;
    correlation_id: string | null;
  };
  type ResearchWorkflowLogEntry = { event_id: number; occurred_at: string; level: "info" | "warning" | "error"; message: string };
  const refresh = useCallback(async () => {
    try {
      const [nextOpportunities, nextDossiers, nextSubjects] = await Promise.all([
        api<Opportunity[]>("/api/v1/research/opportunities"), api<Dossier[]>("/api/v1/research/dossiers"), api<SubjectProfile[]>("/api/v1/subject-profiles"),
      ]);
      const channelSubjects = activeChannelId ? nextSubjects.filter(subject => subject.channel_profile_id === activeChannelId) : nextSubjects;
      const enabledSubjects = channelSubjects.filter(subject => subject.enabled);
      setOpportunities(nextOpportunities); setDossiers(nextDossiers); setSubjects(enabledSubjects); setChannelSubjectIds(channelSubjects.map(subject => subject.id));
      setSelectedSubject(current => enabledSubjects.some(subject => subject.id === current) ? current : enabledSubjects[0]?.id || "");
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Research data could not be loaded"); }
  }, [activeChannelId]);
  useEffect(() => { refresh(); }, [refresh]);
  useEffect(() => { setDetail(null); setSelectedOpportunity(null); setVisibleOpportunityCount(20); }, [activeChannelId]);
  useEffect(() => {
    setSelectedOpportunity(current => {
      if (!current) return current;
      return opportunities.find(item => item.id === current.id) || null;
    });
  }, [opportunities]);
  useEffect(() => {
    if (!run) return;
    api<ResearchWorkflowLogEntry[]>(`/api/v1/research/runs/${encodeURIComponent(run.workflow_id)}/logs`)
      .then(entries => setWorkflowLogs(entries.map(entry => ({ at: entry.occurred_at, message: entry.message, level: entry.level }))))
      .catch(() => undefined);
  }, [run?.workflow_id, run?.state, run?.execution_status]);
  useEffect(() => {
    if (!run || run.execution_status !== "RUNNING") return;
    const source = new EventSource(`/api/v1/research/runs/${encodeURIComponent(run.workflow_id)}/events`);
    function record(next: ResearchWorkflowView) {
      setRun(next);
      setWorkflowLogs(current => {
        const entry = `${next.state} · ${next.progress}% · ${next.execution_status}`;
        if (current.at(-1)?.message === entry) return current;
        return [...current.slice(-19), { at: new Date().toISOString(), message: entry, level: "info" }];
      });
      if (next.execution_status !== "RUNNING") {
        source.close();
        refresh();
      }
    }
    source.addEventListener("status", event => {
      try { record(JSON.parse((event as MessageEvent).data) as ResearchWorkflowView); }
      catch { setMessage("A malformed workflow status event was rejected."); source.close(); }
    });
    source.addEventListener("status-error", event => {
      try {
        const detail = JSON.parse((event as MessageEvent).data) as { message?: string };
        setMessage(detail.message || "Workflow status is temporarily unavailable.");
      } catch { setMessage("Workflow status is temporarily unavailable."); }
      source.close();
    });
    source.onerror = () => { setMessage("The live workflow status stream disconnected; use Refresh status to reconnect."); source.close(); };
    return () => source.close();
  }, [run?.workflow_id, refresh]);
  function acceptRun(next: ResearchWorkflowView, notice: string) {
    setRun(next);
    setWorkflowLogs([{ at: new Date().toISOString(), message: `${next.state} · ${next.progress}% · ${next.execution_status}`, level: "info" }]);
    setMessage(notice);
  }
  async function openDossier(id: string) { try { setDetail(await api<DossierDetail>(`/api/v1/research/dossiers/${id}`)); } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Dossier could not be loaded"); } }
  async function startFixture() {
    try {
      const next = await api<ResearchWorkflowView>("/api/v1/research/fixture-runs", { method: "POST", body: JSON.stringify({ subject_profile_id: selectedSubject, idempotency_key: `ui-${randomUuid()}` }) }, csrf);
      acceptRun(next, `Research workflow ${next.workflow_id} started. Live progress is connected.`);
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Research start failed"); }
  }
  async function startLiveDiscovery() {
    try {
      const next = await api<ResearchWorkflowView>("/api/v1/research/live-discovery-runs", { method: "POST", body: JSON.stringify({ subject_profile_id: selectedSubject, idempotency_key: `ui-${randomUuid()}` }) }, csrf);
      acceptRun(next, `Live discovery workflow ${next.workflow_id} started. Live progress is connected.`);
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Live discovery start failed"); }
  }
  async function decideOpportunity(item: Opportunity, decision: "approved" | "rejected" | "deferred") {
    try {
      const result = await api<{ automatic_continuation?: AutomaticContinuation | null }>(`/api/v1/research/opportunities/${item.id}/decision`, { method: "POST", body: JSON.stringify({ decision, expected_version: item.version, reason: opportunityReason, editorial_rationale: decision === "approved" ? (item.editorial_rationale || opportunityRationale) : null }) }, csrf);
      setMessage(result.automatic_continuation?.message || `Opportunity ${decision}.`); setSelectedOpportunity(null); await refresh();
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Opportunity decision failed"); }
  }
  async function createManualOpportunity(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const form = event.currentTarget; const data = new FormData(form);
    try {
      await api("/api/v1/research/opportunities", { method: "POST", body: JSON.stringify({
        subject_profile_id: data.get("subject_profile_id"), title: data.get("title"),
        summary: data.get("summary"), editorial_rationale: data.get("editorial_rationale"),
        estimated_cost: { tokens: Number(data.get("tokens")), gpu_seconds: 0, currency_minor: 0 },
      }) }, csrf);
      form.reset(); setMessage("Manual opportunity created as pending; it still requires reviewer shortlisting."); await refresh();
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Manual opportunity creation failed"); }
  }
  async function manualDossierCreated(dossier: Dossier) {
    setSelectedOpportunity(null);
    setMessage(`Manual research dossier v${dossier.dossier_version} is ready for review.`);
    await refresh();
    await openDossier(dossier.id);
  }
  async function reviewDossier(decision: "approved" | "rejected") {
    if (!detail) return;
    setDossierReviewPending(decision);
    try {
      const reviewed = await api<Dossier>(`/api/v1/research/dossiers/${detail.id}/review`, { method: "POST", body: JSON.stringify({ decision, expected_version: detail.version, comment: reviewComment, override_reason: decision === "approved" && !detail.completion_evaluation.complete ? overrideReason : null }) }, csrf);
      await openDossier(detail.id); await refresh(); setMessage(reviewed.automatic_continuation?.message || `Dossier ${reviewed.status}. The review decision is recorded.`);
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Dossier review failed"); }
    finally { setDossierReviewPending(null); }
  }
  async function reviewClaim(id: string, version: number, decision: "approved" | "rejected") {
    try {
      await api(`/api/v1/research/claims/${id}/review`, { method: "POST", body: JSON.stringify({ decision, expected_version: version, comment: reviewComment }) }, csrf);
      setMessage(`Claim ${decision}.`); if (detail) await openDossier(detail.id);
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Claim review failed"); }
  }
  async function refreshRun() {
    if (!run) return;
    try {
      const next = await api<ResearchWorkflowView>(`/api/v1/research/runs/${encodeURIComponent(run.workflow_id)}`);
      acceptRun(next, `Workflow status refreshed: ${next.execution_status}.`);
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Workflow status could not be refreshed"); }
  }
  async function cancelRun() {
    if (!run) return;
    try {
      const next = await api<ResearchWorkflowView>(`/api/v1/research/runs/${encodeURIComponent(run.workflow_id)}/cancel`, { method: "POST", body: JSON.stringify({ reason: workflowReason }) }, csrf);
      acceptRun(next, `Cancellation requested for ${run.workflow_id}.`);
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Workflow cancellation failed"); }
  }
  async function retryRun() {
    if (!run) return;
    try {
      const next = await api<ResearchWorkflowView>(`/api/v1/research/runs/${encodeURIComponent(run.workflow_id)}/retry`, { method: "POST", body: JSON.stringify({ idempotency_key: `ui-${randomUuid()}`, reason: workflowReason }) }, csrf);
      acceptRun(next, `Retry ${next.workflow_id} started from ${run.workflow_id}.`);
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Workflow retry failed"); }
  }
  const scopedOpportunities = activeChannelId ? opportunities.filter(item => channelSubjectIds.includes(item.subject_profile_id)) : opportunities;
  const scopedOpportunityIds = new Set(scopedOpportunities.map(item => item.id));
  const scopedDossiers = activeChannelId ? dossiers.filter(item => scopedOpportunityIds.has(item.opportunity_id)) : dossiers;
  const runResult = run?.result || null;
  const nestedSourceHealth = runResult?.source_health && typeof runResult.source_health === "object" && !Array.isArray(runResult.source_health) ? runResult.source_health as Record<string, unknown> : null;
  const displayedSourceHealth = nestedSourceHealth || runResult;
  const searchHealth = typeof runResult?.search_health === "string" ? runResult.search_health : run?.state === "SOURCE_UNAVAILABLE" ? "unavailable" : null;
  const respondingEngines = Array.isArray(displayedSourceHealth?.responding_engines) ? displayedSourceHealth.responding_engines.filter((value): value is string => typeof value === "string") : [];
  const unresponsiveEngines = Array.isArray(displayedSourceHealth?.unresponsive_engines) ? displayedSourceHealth.unresponsive_engines.filter((value): value is Record<string, unknown> => Boolean(value) && typeof value === "object" && !Array.isArray(value)) : [];
  const runStatusClass = run?.execution_status === "FAILED" || searchHealth === "unavailable" ? "bad" : searchHealth === "degraded" ? "waiting" : run?.execution_status === "COMPLETED" ? "good" : "waiting";
  const dossierReviewed = detail?.status === "approved" || detail?.status === "rejected";
  return <><div className="page-heading"><div><p className="eyebrow">Evidence-first discovery</p><h1>Opportunities and dossiers</h1></div><span className="status good">Traceable fixture path online</span></div>
    {message && <div className="notice">{message}</div>}
    {(role === "admin" || role === "operator") && <section className="panel run-bar"><div><h2>Durable discovery runs</h2><p className="muted">Live discovery executes the enabled subject&apos;s search plan through private SearXNG and produces pending opportunities. Search-engine coverage is reported separately from orchestration completion.</p></div><select value={selectedSubject} onChange={event => setSelectedSubject(event.target.value)}><option value="">Select enabled subject</option>{subjects.map(subject => <option key={subject.id} value={subject.id}>{subject.name}</option>)}</select><button className="primary" disabled={!selectedSubject} onClick={startLiveDiscovery}>Run live discovery</button><button className="secondary" disabled={!selectedSubject} onClick={startFixture}>Run fixture acceptance</button><button className="secondary" onClick={refresh}>Refresh board</button>{run && <div className="workflow-monitor"><div><span className={`status ${runStatusClass}`}>{run.state} · {run.progress}%</span><progress max="100" value={run.progress}>{run.progress}%</progress><code>{run.workflow_id}</code>{run.correlation_id && <small>Correlation: {run.correlation_id}</small>}</div><label>Control reason<input value={workflowReason} onChange={event => setWorkflowReason(event.target.value)} minLength={10} /></label><div className="actions"><button className="secondary compact" onClick={refreshRun}>Refresh status</button><button className="secondary compact danger" disabled={run.execution_status !== "RUNNING"} onClick={cancelRun}>Cancel</button><button className="secondary compact" disabled={!run.retryable} onClick={retryRun}>Retry as new run</button></div>{searchHealth && <div className={`discovery-health ${runStatusClass}`} role="status"><strong>Search source health: {searchHealth}</strong><span>{typeof runResult?.raw_result_count === "number" ? `${runResult.raw_result_count} raw results · ` : ""}{respondingEngines.length ? `responding: ${respondingEngines.join(", ")}` : "no responding engine recorded"}</span>{unresponsiveEngines.length > 0 && <small>Unavailable: {unresponsiveEngines.map(item => `${String(item.engine || "engine")} (${String(item.reason || "unavailable")})`).join(" · ")}</small>}{typeof runResult?.fallback_strategy_count === "number" && runResult.fallback_strategy_count > 0 && <small>{runResult.fallback_strategy_count} recent search strateg{runResult.fallback_strategy_count === 1 ? "y used" : "ies used"} the evergreen fallback.</small>}</div>}<div className="workflow-log" aria-live="polite">{workflowLogs.map(entry => <small className={entry.level || "info"} key={`${entry.at}-${entry.message}`}><time>{new Date(entry.at).toLocaleTimeString()}</time> {entry.message}</small>)}</div></div>}</section>}
    {(role === "admin" || role === "editor") && <details className="panel action-disclosure"><summary><span><small>Editor-originated idea</small><strong>Create a manual opportunity</strong><em>Add a hand-picked topic when discovery did not find it.</em></span><span className="chip">pending by default</span></summary><form onSubmit={createManualOpportunity}><label>Enabled subject<select name="subject_profile_id" required>{subjects.map(subject => <option key={subject.id} value={subject.id}>{subject.name}</option>)}</select></label><label>Title<input name="title" minLength={3} required /></label><label>Summary<textarea name="summary" rows={3} minLength={20} required /></label><label>Why does this video deserve to exist?<textarea name="editorial_rationale" rows={3} minLength={20} required /></label><label>Estimated model tokens<input name="tokens" type="number" min="0" defaultValue="0" /></label><button className="secondary" disabled={!subjects.length}>Create pending opportunity</button></form></details>}
    <div className={`research-grid ${scopedDossiers.length ? "" : "solo-primary"}`}><section className="panel"><div className="panel-title"><div><p className="eyebrow">Opportunity board</p><h2>Scored findings</h2></div><span className="chip">Showing {Math.min(visibleOpportunityCount, scopedOpportunities.length)} of {scopedOpportunities.length}</span></div><div className="card-list">{scopedOpportunities.slice(0, visibleOpportunityCount).map(item => <article className="opportunity-card" key={item.id}><div className="score-ring">{item.score ?? "—"}</div><div className="opportunity-card-body"><strong>{item.title}</strong><p>{item.summary}</p><small><strong>Why it deserves to exist:</strong> {item.editorial_rationale || "Required before shortlisting"}</small><div className="score-pills">{item.grouping_reason.filter(reason => reason.startsWith("classification: ")).map(reason => <span key={reason}>{reason.slice("classification: ".length)}</span>)}<span>{item.policy_snapshot.mode || "assisted"} policy</span><span>{item.source_count} source{item.source_count === 1 ? "" : "s"}</span><span>{item.snapshot_count} immutable snapshot{item.snapshot_count === 1 ? "" : "s"}</span>{item.research_state && <span>{item.research_state.replaceAll("_", " ").toLowerCase()}</span>}{Object.entries(item.score_components).slice(0, 3).map(([name, value]) => <span key={name}>{name.replaceAll("_", " ")} {value}</span>)}</div><div className="actions"><button type="button" className="secondary compact control-help" data-usage="Open the complete finding, original sources, score trace, policy gates and review controls without changing its status." aria-haspopup="dialog" aria-controls="opportunity-review-dialog" onClick={() => setSelectedOpportunity(item)}>Review finding</button></div></div><span className={`status ${item.decision === "approved" ? "good" : item.decision === "rejected" ? "bad" : "waiting"}`}>{item.decision}</span></article>)}{!scopedOpportunities.length && <p className="empty">No opportunities have been produced for this channel.</p>}</div>{visibleOpportunityCount < scopedOpportunities.length && <button className="secondary" onClick={() => setVisibleOpportunityCount(count => Math.min(count + 20, scopedOpportunities.length))}>Show 20 more ({scopedOpportunities.length - visibleOpportunityCount} remaining)</button>}</section>
      <section className="panel"><div className="panel-title"><div><p className="eyebrow">Review queue</p><h2>Research dossiers</h2></div><span className="chip">{scopedDossiers.length}</span></div><div className="card-list">{scopedDossiers.map(item => <button className="dossier-card" key={item.id} onClick={() => openDossier(item.id)}><span><strong>{item.executive_summary}</strong><small>Version {item.dossier_version} · {item.status} · readiness round {item.completion_evaluation.explanation_readiness?.enrichment_round ?? "legacy"}</small></span><span className={`status ${item.completion_evaluation.explanation_readiness?.ready ? "good" : "waiting"}`}>{item.completion_evaluation.explanation_readiness?.ready ? "Explanation ready" : "Needs evidence"}</span></button>)}{!scopedDossiers.length && <p className="empty">No dossier is ready for review for this channel.</p>}</div></section></div>
    {selectedOpportunity && <OpportunityReviewDialog opportunity={selectedOpportunity} role={role} csrf={csrf} decisionReason={opportunityReason} rationale={opportunityRationale} onDecisionReasonChange={setOpportunityReason} onRationaleChange={setOpportunityRationale} onClose={() => setSelectedOpportunity(null)} onDecision={decideOpportunity} onManualDossierCreated={manualDossierCreated} />}
    {detail?.ai_evidence_assessment && <section className="panel"><div className="panel-title"><div><p className="eyebrow">AI evidence review · advisory v{detail.ai_evidence_assessment.version}</p><h2>{detail.ai_evidence_assessment.abstained ? "Model abstained" : `${Math.round(detail.ai_evidence_assessment.confidence * 100)}% confidence`}</h2></div><span className="chip">human approval remains required</span></div><p className="muted">The review is bound to exact stored excerpts. It cannot approve claims or override deterministic dossier gates.</p><div className="conclusion-grid"><div><strong>Methodological limits</strong>{detail.ai_evidence_assessment.methodological_limits.map(value => <p key={value}>{value}</p>)}</div><div><strong>Missing counterevidence</strong>{detail.ai_evidence_assessment.counterevidence_gaps.map(value => <p key={value}>{value}</p>)}</div></div><details><summary>Structured claim and source assessments</summary><pre>{JSON.stringify({ claims: detail.ai_evidence_assessment.claim_assessments, sources: detail.ai_evidence_assessment.source_assessments, uncertainty: detail.ai_evidence_assessment.uncertainty }, null, 2)}</pre></details></section>}
    {detail && <section className="panel dossier-detail"><div className="panel-title"><div><p className="eyebrow">Claim ledger</p><h2>{detail.executive_summary}</h2></div><button className="secondary compact" onClick={() => setDetail(null)}>Close</button></div><ReadinessSummary dossier={detail} detailed />{(role === "admin" || role === "reviewer") && (dossierReviewed ? <div className={`notice ${detail.status === "rejected" ? "error" : ""}`} role="status"><strong>Dossier {detail.status}.</strong><p>The human review decision is recorded. Review controls are closed for this dossier version.</p></div> : <div className="review-bar"><label>Review comment<input value={reviewComment} onChange={event => setReviewComment(event.target.value)} minLength={3} disabled={dossierReviewPending !== null} /></label>{!detail.completion_evaluation.complete && <label>Reasoned completion override<input value={overrideReason} onChange={event => setOverrideReason(event.target.value)} minLength={20} disabled={dossierReviewPending !== null} /></label>}<button className="primary" disabled={dossierReviewPending !== null} onClick={() => reviewDossier("approved")}>{dossierReviewPending === "approved" ? "Approving…" : "Approve dossier"}</button><button className="secondary danger" disabled={dossierReviewPending !== null} onClick={() => reviewDossier("rejected")}>{dossierReviewPending === "rejected" ? "Rejecting…" : "Reject dossier"}</button></div>)}<SourceBrowser opportunityId={detail.opportunity_id} csrf={csrf} role={role} /><div className="conclusion-grid"><div><strong>Safe conclusions</strong>{detail.safe_conclusions.map(value => <p key={value}>{value}</p>)}</div><div><strong>Prohibited overstatements</strong>{detail.prohibited_overstatements.map(value => <p key={value}>{value}</p>)}</div></div><div className="claim-list">{detail.claims.map(claim => <article key={claim.id}><header><div><span className="chip">{claim.claim_type} · {claim.risk} risk</span><h3>{claim.normalized_statement}</h3><small>Explanation units: {claim.coverage_unit_ids.length ? claim.coverage_unit_ids.join(", ") : "none"}</small></div><div className="claim-status"><span className={`status ${claim.status === "supported" || claim.status === "approved" ? "good" : "waiting"}`}>{claim.status} · {claim.confidence}%</span>{(role === "admin" || role === "reviewer") && !dossierReviewed && <div><button className="secondary compact" onClick={() => reviewClaim(claim.id, claim.version, "approved")}>Approve</button><button className="secondary compact danger" onClick={() => reviewClaim(claim.id, claim.version, "rejected")}>Reject</button></div>}</div></header><div className="evidence-list">{claim.evidence.map((evidence, index) => <div key={`${evidence.snapshot.content_hash}-${index}`}><span className={`relation ${evidence.relationship}`}>{evidence.relationship}</span><blockquote>{evidence.exact_text}</blockquote><ExternalSourceLink url={evidence.source.canonical_url} title={evidence.source.title} /><code title={evidence.snapshot.content_hash}>{evidence.snapshot.content_hash.slice(0, 16)}…</code></div>)}</div></article>)}</div></section>}
  </>;
}

function ProviderPanel({ csrf }: { csrf: string }) {
  const [providers, setProviders] = useState<Provider[]>([]);
  const [models, setModels] = useState<AIModel[]>([]);
  const [assignments, setAssignments] = useState<TaskAssignment[]>([]);
  const [prompts, setPrompts] = useState<PromptTemplate[]>([]);
  const [usage, setUsage] = useState<AIUsage[]>([]);
  const [selectedProvider, setSelectedProvider] = useState("");
  const [modelQuery, setModelQuery] = useState("");
  const [showAllModels, setShowAllModels] = useState(false);
  const [usageQuery, setUsageQuery] = useState("");
  const [showAllUsage, setShowAllUsage] = useState(false);
  const [discovery, setDiscovery] = useState<ProviderModelDiscovery | null>(null);
  const [selectedDiscoveredModel, setSelectedDiscoveredModel] = useState("");
  const [discoveryBusy, setDiscoveryBusy] = useState(false);
  const [selectedPrompt, setSelectedPrompt] = useState<PromptTemplate | null>(null);
  const [message, setMessage] = useState("");
  const refresh = useCallback(async () => {
    try {
      const [nextProviders, nextModels, nextAssignments, nextPrompts, nextUsage] = await Promise.all([
        api<Provider[]>("/api/v1/providers"), api<AIModel[]>("/api/v1/models"),
        api<TaskAssignment[]>("/api/v1/task-model-assignments"), api<PromptTemplate[]>("/api/v1/prompt-templates"), api<AIUsage[]>("/api/v1/ai-usage"),
      ]);
      setProviders(nextProviders); setModels(nextModels); setAssignments(nextAssignments); setPrompts(nextPrompts); setUsage(nextUsage);
      setSelectedProvider(current => current || nextProviders[0]?.id || "");
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Provider configuration could not be loaded"); }
  }, []);
  useEffect(() => { refresh(); }, [refresh]);
  async function createProvider(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const form = event.currentTarget; const data = new FormData(form); const driver = String(data.get("driver_type"));
    try {
      await api("/api/v1/providers", { method: "POST", body: JSON.stringify({
        slug: data.get("slug"), name: data.get("name"), driver_type: driver,
        endpoint: driver === "fake" ? null : String(data.get("endpoint") || ""), enabled: data.get("enabled") === "on",
        location: driver === "fake" ? "local" : data.get("location"), authentication_scheme: driver === "fake" ? "none" : data.get("authentication_scheme"),
        secret_reference: driver === "fake" || data.get("authentication_scheme") === "none" ? null : String(data.get("secret_reference") || ""),
        data_policy: driver === "fake" ? "local_only" : data.get("data_policy"), residency_policy: {}, capabilities: {}, concurrency_limit: Number(data.get("concurrency_limit")), requests_per_minute: Number(data.get("requests_per_minute")),
      }) }, csrf); form.reset(); setMessage("Provider created. Secrets remain mounted references and are never returned by the API."); await refresh();
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Provider creation failed"); }
  }
  async function createModel(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const form = event.currentTarget; const data = new FormData(form); const tasks = String(data.get("tasks") || "").split(",").map(value => value.trim()).filter(Boolean);
    try {
      await api("/api/v1/models", { method: "POST", body: JSON.stringify({ provider_id: data.get("provider_id"), model_name: data.get("model_name"), display_name: data.get("display_name"), visible: true, enabled: true, model_version: data.get("model_version"), capabilities: { tasks, structured_output: true }, context_limit: Number(data.get("context_limit")), output_limit: Number(data.get("output_limit")), cost_policy: {}, data_policy_override: null }) }, csrf);
      form.reset(); setMessage("Visible model registered."); await refresh();
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Model creation failed"); }
  }
  async function discoverModels() {
    if (!selectedProvider) return;
    setDiscoveryBusy(true);
    try {
      const result = await api<ProviderModelDiscovery>(`/api/v1/providers/${selectedProvider}/discover-models`, { method: "POST" }, csrf);
      setDiscovery(result);
      setSelectedDiscoveredModel(result.models[0] || "");
      setMessage(`${result.model_count} models discovered from ${result.provider_name}. Select one and assign its allowed tasks.`);
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Provider model discovery failed"); }
    finally { setDiscoveryBusy(false); }
  }
  async function importDiscoveredModel(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!discovery || !selectedDiscoveredModel) return;
    const data = new FormData(event.currentTarget);
    const tasks = String(data.get("discovered_tasks") || "").split(",").map(value => value.trim()).filter(Boolean);
    try {
      await api("/api/v1/models", { method: "POST", body: JSON.stringify({
        provider_id: discovery.provider_id, model_name: selectedDiscoveredModel,
        display_name: selectedDiscoveredModel, visible: true, enabled: true,
        model_version: "discovered", capabilities: { tasks, structured_output: true },
        context_limit: Number(data.get("discovered_context_limit")),
        output_limit: Number(data.get("discovered_output_limit")), cost_policy: {}, data_policy_override: null,
      }) }, csrf);
      setMessage(`${selectedDiscoveredModel} imported. It is now selectable for: ${tasks.join(", ")}.`);
      await refresh();
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Discovered model import failed"); }
  }
  async function activateRoute(task: string, modelId: string) {
    try {
      await api(`/api/v1/task-model-assignments/${task}`, { method: "PUT", body: JSON.stringify({ task_type: task, primary_model_id: modelId, fallback_model_ids: [], routing_policy: { repair_attempts: 2 }, budget_policy: { max_output_tokens: models.find(item => item.id === modelId)?.output_limit || 2048 }, comment: "Activated through the provider routing UI" }) }, csrf);
      setMessage(`${task.replaceAll("_", " ")} routing activated as a new immutable version.`); await refresh();
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Routing activation failed"); }
  }
  async function savePrompt(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!selectedPrompt) return; const data = new FormData(event.currentTarget);
    try {
      await api(`/api/v1/prompt-templates/${selectedPrompt.template_key}`, { method: "PUT", body: JSON.stringify({ template_key: selectedPrompt.template_key, task_type: selectedPrompt.task_type, system_instructions: data.get("system_instructions"), template: data.get("template"), input_schema: JSON.parse(String(data.get("input_schema"))), response_schema: JSON.parse(String(data.get("response_schema"))), comment: String(data.get("comment")) }) }, csrf);
      setMessage("Prompt validated and activated as a new immutable version."); setSelectedPrompt(null); await refresh();
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Prompt save failed"); }
  }
  const activeAssignments: Record<string, TaskAssignment> = Object.fromEntries(assignments.filter(item => item.active).map(item => [item.task_type, item]));
  const priorPrompt = selectedPrompt ? prompts.find(item => item.template_key === selectedPrompt.template_key && item.template_version === selectedPrompt.template_version - 1) : null;
  const normalizedModelQuery = modelQuery.trim().toLowerCase();
  const filteredModels = models.filter(item => {
    const providerName = providers.find(provider => provider.id === item.provider_id)?.name || "";
    return !normalizedModelQuery || [item.display_name, item.model_name, providerName, ...(item.capabilities.tasks || [])].some(value => value.toLowerCase().includes(normalizedModelQuery));
  });
  const visibleModels = showAllModels ? filteredModels : filteredModels.slice(0, 12);
  const normalizedUsageQuery = usageQuery.trim().toLowerCase();
  const filteredUsage = usage.filter(item => {
    const searchable = [item.task_type, item.provider_name, item.model_name, item.workflow_id, item.activity_id, item.correlation_id, item.request_hash, item.response_hash];
    return !normalizedUsageQuery || searchable.some(value => value.toLowerCase().includes(normalizedUsageQuery));
  });
  const visibleUsage = showAllUsage ? filteredUsage : filteredUsage.slice(0, 20);
  return <><div className="page-heading"><div><p className="eyebrow">Policy-enforced model gateway</p><h1>Providers, routing and prompts</h1></div><span className="status good">No raw secrets exposed</span></div>{message && <div className="notice">{message}</div>}
    <div className="split-grid"><section className="panel"><div className="panel-title"><div><p className="eyebrow">Registered endpoints</p><h2>Providers</h2></div><span className="chip">{providers.length}</span></div><div className="card-list">{providers.map(item => <article className="profile-card subject" key={item.id}><div><strong>{item.name}</strong><small>{item.driver_type} · {item.location} · {item.data_policy}</small><small>{item.endpoint || "No network endpoint"} · {item.has_secret ? "mounted secret" : "credential free"}</small></div><span className={`status ${item.enabled ? "good" : ""}`}>{item.enabled ? "Enabled" : "Disabled"}</span></article>)}</div><form onSubmit={createProvider}><div className="form-pair"><label>Name<input name="name" required /></label><label>Slug<input name="slug" pattern="[a-z0-9]+(?:-[a-z0-9]+)*" required /></label></div><div className="form-pair"><label>Driver<select name="driver_type" defaultValue="fake"><option value="fake">Fake / CI</option><option value="openai_compatible">OpenAI-compatible</option><option value="ollama">Ollama native</option><option value="anthropic">Anthropic style</option><option value="gemini">Gemini style</option><option value="generic_rest">Generic REST/JSON</option></select></label><label>Location<select name="location" defaultValue="local"><option value="local">Local</option><option value="remote">Remote</option></select></label></div><label>Endpoint URL<input name="endpoint" placeholder="https://provider.example/v1" /></label><div className="form-pair"><label>Authentication<select name="authentication_scheme" defaultValue="none"><option value="none">None</option><option value="bearer">Bearer</option><option value="api_key">API key</option><option value="oauth">OAuth token</option></select></label><label>Mounted secret reference<input name="secret_reference" /></label></div><label>Data policy<select name="data_policy" defaultValue="local_only"><option value="local_only">Local only</option><option value="remote_after_redaction">Remote after redaction</option><option value="remote_allowed">Remote allowed</option></select></label><div className="form-pair"><label>Concurrency<input name="concurrency_limit" type="number" min="1" defaultValue="1" /></label><label>Requests / minute<input name="requests_per_minute" type="number" min="1" defaultValue="60" /></label></div><label className="checkbox"><input name="enabled" type="checkbox" /> Enable after creation</label><button className="primary">Create provider</button></form></section>
      <section className="panel"><div className="panel-title"><div><p className="eyebrow">Visible inventory</p><h2>Models</h2></div><span className="chip">{filteredModels.length} of {models.length}</span></div><div className="model-toolbar"><label>Find model<input type="search" value={modelQuery} onChange={event => { setModelQuery(event.target.value); setShowAllModels(false); }} placeholder="Name, provider or task" /></label><button type="button" className="secondary" disabled={filteredModels.length <= 12} onClick={() => setShowAllModels(value => !value)}>{showAllModels ? "Show fewer" : `Show all ${filteredModels.length}`}</button></div><div className="card-list compact-list">{visibleModels.map(item => <article className="profile-card subject" key={item.id}><div><strong>{item.display_name}</strong><small>{providers.find(provider => provider.id === item.provider_id)?.name} · {item.model_name}</small><small>{item.capabilities.tasks?.join(" · ") || "No task restriction"} · {item.context_limit.toLocaleString()} context</small></div><span className={`status ${item.enabled && item.visible ? "good" : ""}`}>{item.enabled && item.visible ? "Routable" : "Hidden"}</span></article>)}{!filteredModels.length && <p className="empty">No model matches this search.</p>}</div>{filteredModels.length > visibleModels.length && <p className="muted">Showing the first {visibleModels.length}. Search by model, provider or task to narrow the list.</p>}<div className="actions"><button type="button" className="secondary control-help" data-usage="Ask the selected provider for its current model inventory through the isolated Editorial Worker." disabled={!selectedProvider || discoveryBusy} onClick={() => void discoverModels()}>{discoveryBusy ? "Discovering…" : "Discover provider models"}</button>{discovery && <span className="chip">{discovery.model_count} available on server</span>}</div>{discovery && <form onSubmit={importDiscoveredModel}><label>Available provider model<select value={selectedDiscoveredModel} onChange={event => setSelectedDiscoveredModel(event.target.value)} required>{discovery.models.map(name => <option key={name} value={name}>{name}{models.some(item => item.provider_id === discovery.provider_id && item.model_name === name) ? " · already registered" : ""}</option>)}</select></label><label>Allowed tasks<input name="discovered_tasks" defaultValue="script_writer, script_verifier, storyboard, topic_qualifier, research_query_planner, evidence_synthesizer, evidence_reviewer" required /></label><div className="form-pair"><label>Context limit<input name="discovered_context_limit" type="number" min="256" defaultValue="65536" /></label><label>Output limit<input name="discovered_output_limit" type="number" min="64" defaultValue="4096" /></label></div><button className="primary" disabled={!selectedDiscoveredModel || models.some(item => item.provider_id === discovery.provider_id && item.model_name === selectedDiscoveredModel)}>Import selected server model</button></form>}<details><summary>Register a model manually</summary><form onSubmit={createModel}><label>Provider<select name="provider_id" value={selectedProvider} onChange={event => setSelectedProvider(event.target.value)} required><option value="">Select provider</option>{providers.filter(item => item.enabled).map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label><div className="form-pair"><label>Display name<input name="display_name" required /></label><label>Provider model name<input name="model_name" required /></label></div><div className="form-pair"><label>Model version<input name="model_version" defaultValue="1" required /></label><label>Enabled tasks<input name="tasks" placeholder="script_writer, storyboard" required /></label></div><div className="form-pair"><label>Context limit<input name="context_limit" type="number" min="256" defaultValue="32768" /></label><label>Output limit<input name="output_limit" type="number" min="64" defaultValue="8192" /></label></div><button className="primary" disabled={!selectedProvider}>Register visible model</button></form></details></section></div>
    <section className="panel"><div className="panel-title"><div><p className="eyebrow">Human-controlled selection</p><h2>Task routing</h2></div><span className="muted">Approved recommendations apply as a new immutable route version.</span></div><div className="route-grid">{["script_writer", "script_verifier", "storyboard", "topic_qualifier", "research_query_planner", "evidence_synthesizer", "evidence_reviewer"].map(task => <label key={task}>{task.replaceAll("_", " ")}<select value={activeAssignments[task]?.primary_model_id || ""} onChange={event => activateRoute(task, event.target.value)}><option value="">Not configured</option>{models.filter(item => item.enabled && item.visible && (!item.capabilities.tasks?.length || item.capabilities.tasks.includes(task))).map(item => <option key={item.id} value={item.id}>{item.display_name}</option>)}</select><small>{activeAssignments[task] ? `Assignment v${activeAssignments[task].assignment_version}` : "Blocked until configured"}</small></label>)}</div></section>
    <section className="panel"><div className="panel-title"><div><p className="eyebrow">Immutable prompt registry</p><h2>Active prompts</h2></div><span className="chip">{prompts.filter(item => item.active).length}</span></div><div className="card-list">{prompts.filter(item => item.active).map(item => <button className="dossier-card" key={item.id} onClick={() => setSelectedPrompt(item)}><span><strong>{item.template_key}</strong><small>{item.task_type} · version {item.template_version} · {item.content_hash.slice(0, 12)}…</small></span><span className="status good">Active</span></button>)}</div>{selectedPrompt && <div className="split-grid"><form onSubmit={savePrompt}><label>System instructions<textarea name="system_instructions" rows={5} defaultValue={selectedPrompt.system_instructions} required /></label><label>Template<textarea name="template" rows={7} defaultValue={selectedPrompt.template} required /></label><div className="form-pair"><label>Input JSON Schema<textarea name="input_schema" rows={10} defaultValue={JSON.stringify(selectedPrompt.input_schema, null, 2)} /></label><label>Response JSON Schema<textarea name="response_schema" rows={10} defaultValue={JSON.stringify(selectedPrompt.response_schema, null, 2)} /></label></div><label>Version comment<input name="comment" defaultValue="Reviewed and activated through the prompt UI" minLength={3} required /></label><div className="actions"><button className="primary">Validate & activate</button><button type="button" className="secondary" onClick={() => setSelectedPrompt(null)}>Cancel</button></div></form><div><p className="eyebrow">Version comparison</p><h3>{priorPrompt ? `Prior v${priorPrompt.template_version}` : "No prior version"}</h3>{priorPrompt ? <><label>Prior system instructions<textarea readOnly rows={5} value={priorPrompt.system_instructions} /></label><label>Prior template<textarea readOnly rows={7} value={priorPrompt.template} /></label><small>Prior {priorPrompt.content_hash.slice(0, 16)}… · current {selectedPrompt.content_hash.slice(0, 16)}…</small></> : <p className="empty">This is the first immutable version.</p>}</div></div>}</section>
    <section className="panel"><div className="panel-title"><div><p className="eyebrow">Safe usage ledger</p><h2>Model calls and deterministic redactions</h2></div><span className="chip">{filteredUsage.length} of {usage.length}</span></div><div className="model-toolbar"><label>Find model call<input type="search" value={usageQuery} onChange={event => { setUsageQuery(event.target.value); setShowAllUsage(false); }} placeholder="Task, model, provider, workflow or hash" /></label><button type="button" className="secondary" disabled={filteredUsage.length <= 20} onClick={() => setShowAllUsage(value => !value)}>{showAllUsage ? "Show fewer" : `Show all ${filteredUsage.length}`}</button></div><div className="card-list compact-list">{visibleUsage.map(item => <article className="profile-card subject" key={item.id}><div><strong>{item.task_type.replaceAll("_", " ")} · {item.model_name}</strong><small>{item.provider_name} · {item.input_tokens} in / {item.output_tokens} out · {item.latency_ms} ms</small><small>Correlation {item.correlation_id} · request {item.request_hash.slice(0, 12)}…</small>{item.redaction_summary.removed?.map(redaction => <small key={`${item.id}-${redaction.path}-${redaction.value_hash}`}>Removed {redaction.category} at {redaction.path} · hash {redaction.value_hash.slice(0, 12)}…</small>)}</div><span className={`status ${item.redaction_summary.count ? "waiting" : "good"}`}>{item.redaction_summary.count || 0} redactions</span></article>)}{!filteredUsage.length && <p className="empty">{usage.length ? "No model call matches this search." : "No model call has been recorded."}</p>}</div>{filteredUsage.length > visibleUsage.length && <p className="muted">Showing the first {visibleUsage.length}. Search by task, model, provider or hash to narrow the ledger.</p>}</section>
  </>;
}

function EditorialPanel({ csrf, role, activeChannelId }: { csrf: string; role: Role; activeChannelId: string }) {
  const [dossiers, setDossiers] = useState<Dossier[]>([]);
  const [scripts, setScripts] = useState<ScriptSummary[]>([]);
  const [storyboards, setStoryboards] = useState<StoryboardSummary[]>([]);
  const [opportunities, setOpportunities] = useState<Opportunity[]>([]);
  const [channelSubjectIds, setChannelSubjectIds] = useState<string[]>([]);
  const [script, setScript] = useState<ScriptDetail | null>(null);
  const [edited, setEdited] = useState<ScriptDetail | null>(null);
  const [dossier, setDossier] = useState<DossierDetail | null>(null);
  const [storyboard, setStoryboard] = useState<StoryboardDetail | null>(null);
  const [sceneJson, setSceneJson] = useState("");
  const [selectedScene, setSelectedScene] = useState<string | null>(null);
  const [sceneAlternatives, setSceneAlternatives] = useState<SceneAlternative[]>([]);
  const [selectedSegmentKeys, setSelectedSegmentKeys] = useState<string[]>([]);
  const [run, setRun] = useState<EditorialRun | null>(null);
  const [message, setMessage] = useState("");
  const [importOpportunityId, setImportOpportunityId] = useState("");
  const [importTitle, setImportTitle] = useState("");
  const [importText, setImportText] = useState("");
  const [importResearch, setImportResearch] = useState<DossierDetail | null>(null);
  const [directTitle, setDirectTitle] = useState("");
  const [directText, setDirectText] = useState("");
  const [directPreview, setDirectPreview] = useState<DirectScriptedVideoPreview | null>(null);
  const [comment, setComment] = useState("Reviewed against exact claims, evidence, warnings, and the current immutable hash.");
  const refresh = useCallback(async () => {
    try {
      const [nextDossiers, nextScripts, nextStoryboards, nextOpportunities, nextSubjects] = await Promise.all([
        api<Dossier[]>("/api/v1/research/dossiers"), api<ScriptSummary[]>("/api/v1/editorial/scripts"), api<StoryboardSummary[]>("/api/v1/editorial/storyboards"),
        api<Opportunity[]>("/api/v1/research/opportunities"), api<SubjectProfile[]>("/api/v1/subject-profiles"),
      ]);
      setDossiers(nextDossiers); setScripts(nextScripts); setStoryboards(nextStoryboards); setOpportunities(nextOpportunities);
      setChannelSubjectIds((activeChannelId ? nextSubjects.filter(item => item.channel_profile_id === activeChannelId) : nextSubjects).map(item => item.id));
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Editorial production data could not be loaded"); }
  }, [activeChannelId]);
  useEffect(() => { refresh(); }, [refresh]);
  useEffect(() => { setScript(null); setEdited(null); setDossier(null); setStoryboard(null); }, [activeChannelId]);
  useEffect(() => {
    if (!run || run.execution_status !== "RUNNING") return;
    const source = new EventSource(`/api/v1/editorial/runs/${encodeURIComponent(run.workflow_id)}/events`);
    source.addEventListener("status", event => {
      try {
        const next = JSON.parse((event as MessageEvent).data) as EditorialRun; setRun(next);
        if (next.execution_status !== "RUNNING") {
          source.close(); refresh(); setMessage(`${next.state}: ${next.execution_status}.`);
          if (script) api<ScriptDetail>(`/api/v1/editorial/scripts/${script.id}`).then(value => { setScript(value); setEdited(structuredClone(value)); });
          if (storyboard) api<StoryboardDetail>(`/api/v1/editorial/storyboards/${storyboard.id}`).then(setStoryboard);
          if (storyboard && selectedScene) api<SceneAlternative[]>(`/api/v1/editorial/storyboards/${storyboard.id}/scenes/${selectedScene}/alternatives`).then(setSceneAlternatives);
        }
      } catch { source.close(); setMessage("Malformed editorial workflow event rejected."); }
    });
    source.onerror = () => { source.close(); setMessage("Editorial workflow stream disconnected; refresh the page to reconcile status."); };
    return () => source.close();
  }, [run?.workflow_id, refresh, script?.id, storyboard?.id, selectedScene]);
  async function openScript(id: string) {
    try {
      const detail = await api<ScriptDetail>(`/api/v1/editorial/scripts/${id}`); setScript(detail); setEdited(structuredClone(detail)); setSelectedSegmentKeys([]);
      setDossier(detail.dossier_id ? await api<DossierDetail>(`/api/v1/research/dossiers/${detail.dossier_id}`) : null);
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Script could not be loaded"); }
  }
  async function selectImportOpportunity(opportunityId: string, importable: Dossier[]) {
    setImportOpportunityId(opportunityId); setImportResearch(null);
    const opportunity = opportunities.find(item => item.id === opportunityId);
    if (opportunity) setImportTitle(opportunity.title);
    const selected = importable.find(item => item.opportunity_id === opportunityId);
    if (!selected) return;
    try { setImportResearch(await api<DossierDetail>(`/api/v1/research/dossiers/${selected.id}`)); }
    catch (caught) { setMessage(caught instanceof Error ? caught.message : "Existing research could not be loaded"); }
  }
  async function importExistingResearchScript(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    try {
      const next = await api<EditorialRun>("/api/v1/editorial/script-import-runs", { method: "POST", body: JSON.stringify({ opportunity_id: importOpportunityId, title: importTitle, script_text: importText, sensitivity: "internal", idempotency_key: `ui-${randomUuid()}` }) }, csrf);
      setRun(next); setMessage("The pasted script is being adapted to the exact approved claims and independently verified. Unsupported factual details cannot pass through as evidence.");
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Pasted script import failed"); }
  }
  async function previewDirectScriptedVideo() {
    try {
      const next = await api<DirectScriptedVideoPreview>("/api/v1/editorial/direct-scripted-video-preview", { method: "POST", body: JSON.stringify({ title: directTitle, master_script: directText, target_wpm_min: 108, target_wpm_max: 116 }) }, csrf);
      setDirectPreview(next); setMessage(`${next.scene_count} scenes parsed. Evidence checking is not part of this workflow.`);
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Direct scripted-video preview failed"); }
  }
  async function importDirectScriptedVideo(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!activeChannelId) { setMessage("Select a channel before importing a direct scripted video."); return; }
    try {
      const next = await api<EditorialRun>("/api/v1/editorial/direct-scripted-video-runs", { method: "POST", body: JSON.stringify({ channel_profile_id: activeChannelId, title: directTitle, master_script: directText, target_wpm_min: 108, target_wpm_max: 116, sensitivity: "internal", idempotency_key: `ui-${randomUuid()}` }) }, csrf);
      setRun(next); setMessage("Direct scripted-video import started. TubeFactory will validate structure only; factual approval remains with the reviewer.");
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Direct scripted-video import failed"); }
  }
  async function openStoryboard(id: string) {
    try { const detail = await api<StoryboardDetail>(`/api/v1/editorial/storyboards/${id}`); setStoryboard(detail); setSelectedScene(null); setSceneAlternatives([]); }
    catch (caught) { setMessage(caught instanceof Error ? caught.message : "Storyboard could not be loaded"); }
  }
  async function verifyEditedScript() {
    if (!script) return;
    try {
      const next = await api<EditorialRun>(`/api/v1/editorial/scripts/${script.id}/verification-runs`, { method: "POST", body: JSON.stringify({ expected_version: script.version, expected_hash: script.content_hash, sensitivity: "internal", idempotency_key: `ui-${randomUuid()}` }) }, csrf);
      setRun(next); setMessage("Independent verifier started for the exact edited draft.");
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Script verification failed"); }
  }
  async function regenerateSelection() {
    if (!script || !selectedSegmentKeys.length) return;
    try {
      const next = await api<EditorialRun>(`/api/v1/editorial/scripts/${script.id}/regeneration-runs`, { method: "POST", body: JSON.stringify({ expected_version: script.version, expected_hash: script.content_hash, segment_keys: selectedSegmentKeys, instruction: comment, sensitivity: "internal", idempotency_key: `ui-${randomUuid()}` }) }, csrf);
      setRun(next); setSelectedSegmentKeys([]); setMessage("Selected unlocked segments are regenerating and independently verifying into a new immutable version; every unselected and locked segment is preserved byte-for-byte.");
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Selected segment regeneration failed"); }
  }
  async function saveScript() {
    if (!script || !edited) return;
    try {
      const next = await api<ScriptDetail>(`/api/v1/editorial/scripts/${script.id}/versions`, { method: "POST", body: JSON.stringify({ expected_version: script.version, expected_hash: script.content_hash, title: edited.title, comment, segments: edited.segments.map(item => ({ segment_key: item.segment_key, segment_type: item.segment_type, narration: item.narration, presentation_purpose: item.presentation_purpose, duration_seconds: item.duration_seconds, citation_display: item.citation_display, annotations: item.annotations, locked: item.locked })) }) }, csrf);
      setScript(next); setEdited(structuredClone(next)); setMessage(next.status === "blocked" ? "New immutable version is blocked. Review its warnings." : next.evidence_required ? "New immutable draft saved. Independent verification is now required." : "New direct scripted-video version saved and remains ready for approval."); await refresh();
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Script version could not be saved"); }
  }
  function editNarration(index: number, narration: string) {
    if (!edited) return; const next = structuredClone(edited); const segment = next.segments[index]; segment.narration = narration;
    if (segment.annotations.length === 1) { segment.annotations[0].text = narration; segment.annotations[0].start_offset = 0; segment.annotations[0].end_offset = narration.length; }
    setEdited(next);
  }
  async function approveScript() {
    if (!script) return;
    try { const next = await api<ScriptDetail>(`/api/v1/editorial/scripts/${script.id}/approve`, { method: "POST", body: JSON.stringify({ expected_version: script.version, expected_hash: script.content_hash, comment }) }, csrf); setScript(next); setEdited(structuredClone(next)); setMessage(next.automatic_continuation?.message || "Exact verified script version approved for storyboarding."); await refresh(); }
    catch (caught) { setMessage(caught instanceof Error ? caught.message : "Script approval failed"); }
  }
  async function setSceneLock(item: StoryboardDetail["scenes"][number], locked: boolean) {
    if (!storyboard) return;
    try { const next = await api<StoryboardDetail>(`/api/v1/editorial/storyboards/${storyboard.id}/scenes/${item.id}/lock`, { method: "PUT", body: JSON.stringify({ expected_version: item.aggregate_version, locked, comment }) }, csrf); setStoryboard(next); setMessage(`Scene ${locked ? "locked" : "unlocked"}.`); }
    catch (caught) { setMessage(caught instanceof Error ? caught.message : "Scene lock failed"); }
  }
  async function openSceneEditor(item: StoryboardDetail["scenes"][number]) {
    if (!storyboard) return;
    setSelectedScene(item.id); setSceneJson(JSON.stringify(item.scene_spec, null, 2));
    try { setSceneAlternatives(await api<SceneAlternative[]>(`/api/v1/editorial/storyboards/${storyboard.id}/scenes/${item.id}/alternatives`)); }
    catch (caught) { setMessage(caught instanceof Error ? caught.message : "Scene alternatives could not be loaded"); }
  }
  async function generateSceneAlternative(item: StoryboardDetail["scenes"][number]) {
    if (!storyboard) return;
    try {
      setSelectedScene(item.id); setSceneJson(JSON.stringify(item.scene_spec, null, 2));
      setSceneAlternatives(await api<SceneAlternative[]>(`/api/v1/editorial/storyboards/${storyboard.id}/scenes/${item.id}/alternatives`));
      const next = await api<EditorialRun>(`/api/v1/editorial/storyboards/${storyboard.id}/scenes/${item.id}/alternative-runs`, { method: "POST", body: JSON.stringify({ expected_scene_version: item.version, expected_storyboard_version: storyboard.version, instruction: comment, sensitivity: "internal", idempotency_key: `ui-${randomUuid()}` }) }, csrf);
      setRun(next); setMessage("A model-generated SceneSpec alternative is being validated against the complete storyboard and exact source links.");
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Scene alternative generation failed"); }
  }
  async function selectSceneAlternative(item: SceneAlternative) {
    if (!storyboard || !selectedScene) return; const scene = storyboard.scenes.find(value => value.id === selectedScene); if (!scene) return;
    try {
      const next = await api<StoryboardDetail>(`/api/v1/editorial/storyboards/${storyboard.id}/scenes/${scene.id}/alternatives/${item.id}/select`, { method: "POST", body: JSON.stringify({ expected_scene_version: scene.version, expected_storyboard_version: storyboard.version, comment }) }, csrf);
      setStoryboard(next); setSelectedScene(null); setSceneAlternatives([]); setMessage("Selected alternative created a complete new immutable storyboard version; prior approval no longer applies."); await refresh();
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Scene alternative could not be selected"); }
  }
  async function saveScene() {
    if (!storyboard || !selectedScene) return; const item = storyboard.scenes.find(value => value.id === selectedScene); if (!item) return;
    try { const next = await api<StoryboardDetail>(`/api/v1/editorial/storyboards/${storyboard.id}/scenes/${item.id}/versions`, { method: "POST", body: JSON.stringify({ expected_scene_version: item.version, expected_storyboard_version: storyboard.version, scene_spec: JSON.parse(sceneJson), comment }) }, csrf); setStoryboard(next); setSelectedScene(null); setSceneAlternatives([]); setMessage("New immutable storyboard and scene versions created; prior approval no longer applies."); await refresh(); }
    catch (caught) { setMessage(caught instanceof Error ? caught.message : "Scene version could not be saved"); }
  }
  async function approveStoryboard() {
    if (!storyboard) return;
    try { const next = await api<StoryboardDetail>(`/api/v1/editorial/storyboards/${storyboard.id}/approve`, { method: "POST", body: JSON.stringify({ expected_version: storyboard.version, expected_hash: storyboard.content_hash, comment }) }, csrf); setStoryboard(next); setMessage(next.automatic_continuation?.message || "Exact storyboard version approved."); await refresh(); }
    catch (caught) { setMessage(caught instanceof Error ? caught.message : "Storyboard approval failed"); }
  }
  const claims: Record<string, DossierDetail["claims"][number]> = Object.fromEntries((dossier?.claims || []).map(item => [item.id, item]));
  const changedSegments = edited && script ? edited.segments.filter((item, index) => JSON.stringify(item) !== JSON.stringify(script.segments[index])).map(item => item.segment_key) : [];
  const scopedOpportunityIds = new Set((activeChannelId ? opportunities.filter(item => channelSubjectIds.includes(item.subject_profile_id)) : opportunities).map(item => item.id));
  const scopedDossiers = activeChannelId ? dossiers.filter(item => scopedOpportunityIds.has(item.opportunity_id)) : dossiers;
  const scopedDossierIds = new Set(scopedDossiers.map(item => item.id));
  const scopedScripts = activeChannelId ? scripts.filter(item => item.channel_profile_id === activeChannelId || (item.dossier_id ? scopedDossierIds.has(item.dossier_id) : false)) : scripts;
  const scopedScriptIds = new Set(scopedScripts.map(item => item.id));
  const scopedStoryboards = activeChannelId ? storyboards.filter(item => scopedScriptIds.has(item.script_id)) : storyboards;
  const eligibleDossiers = scopedDossiers.filter(item => item.status === "approved" && item.completion_evaluation.explanation_readiness?.ready);
  const blockedApprovedDossiers = scopedDossiers.filter(item => item.status === "approved" && !item.completion_evaluation.explanation_readiness?.ready);
  const unstartedDossiers = eligibleDossiers.filter(item => !scopedScripts.some(value => value.dossier_id === item.id));
  const importableDossiers = eligibleDossiers
    .filter(item => {
      const existing = scopedScripts.find(value => value.dossier_id === item.id);
      return !existing || (existing.status !== "approved" && !scopedStoryboards.some(value => value.script_id === existing.id));
    })
    .sort((left, right) => right.dossier_version - left.dossier_version)
    .filter((item, index, values) => values.findIndex(value => value.opportunity_id === item.opportunity_id) === index);
  const importPrompt = `Du bist Autor:in für ein deutschsprachiges FaktischSimpel-Erklärvideo. Erstelle aus dem unten eingefügten, bereits recherchierten Quellenbrief ein natürlich gesprochenes Script für etwa 5–10 Minuten.\n\nZielgruppe: interessierte Allgemeinheit ohne vorausgesetztes Fachwissen. Sprich die Zuschauer mit Du an. Beginne verständlich, entwickle jeden Gedanken schrittweise und erkläre Fachbegriffe vor ihrer Verwendung. Nutze anschauliche Beispiele nur zur Erklärung, nicht als Beleg. Unterscheide klar zwischen gut belegtem Wissen, Einordnung und offenen Fragen.\n\nVerwende ausschließlich Aussagen, die der Quellenbrief trägt. Erfinde keine Fakten, Studien, Zahlen, Zitate oder Quellen. Wenn etwas nicht ausreichend belegt ist, lasse es weg oder kennzeichne es als offene Frage. Übernimm keine internen Claim-IDs in den gesprochenen Text.\n\nGib nur Folgendes aus:\nTITEL: <präziser Arbeitstitel>\nSCRIPT:\n<vollständiger, zusammenhängender Sprechertext ohne Tabellen, JSON oder Produktionsanweisungen>\n\nQUELLENBRIEF:\n${importResearch ? `Thema: ${opportunities.find(item => item.id === importResearch.opportunity_id)?.title || importTitle}\nZusammenfassung: ${importResearch.executive_summary}\nSichere Schlussfolgerungen:\n${importResearch.safe_conclusions.map(item => `- ${item}`).join("\n")}\nNicht zulässige Übertreibungen:\n${importResearch.prohibited_overstatements.map(item => `- ${item}`).join("\n")}\nOffene Fragen:\n${importResearch.unresolved_questions.map(item => `- ${item}`).join("\n")}\nFreigegebene Aussagen:\n${importResearch.claims.filter(item => item.status === "approved").map(item => `- ${item.normalized_statement}`).join("\n")}` : "[Wähle zuerst eine Opportunity mit abgeschlossenem Quellenbrief und füge den angezeigten Forschungsinhalt hier ein.]"}`;
  return <><div className="page-heading"><div><p className="eyebrow">Increment 2 editorial production</p><h1>Scripts and storyboards</h1></div><span className="status good">Sentence-level evidence gate online</span></div>{message && <div className="notice">{message}</div>}{run && <section className="panel workflow-monitor"><div><span className={`status ${run.execution_status === "COMPLETED" ? "good" : "waiting"}`}>{run.state} · {run.progress}%</span><progress max="100" value={run.progress}>{run.progress}%</progress><code>{run.workflow_id}</code></div></section>}
    <details className="panel action-disclosure"><summary><span><small>Direct scripted video</small><strong>Paste master script and scene direction</strong><em>No research, dossier, claim binding or evidence checking. Script and storyboard approval still gate production.</em></span><span className="chip">{activeChannelId ? "Channel selected" : "Select channel first"}</span></summary>{(role === "admin" || role === "editor") ? <form onSubmit={importDirectScriptedVideo}><label>Production title<input value={directTitle} onChange={event => setDirectTitle(event.target.value)} maxLength={300} required /></label><label>Master script and scene direction<textarea value={directText} onChange={event => { setDirectText(event.target.value); setDirectPreview(null); }} minLength={200} maxLength={100000} rows={16} placeholder="S01 | 00:00–00:54 | Scene title&#10;&#10;Voiceover: ...&#10;Bild/Schnitt: ...&#10;On-Screen-Text: ...&#10;Benötigte Assets: ...&#10;Evidenz-/Freigabehinweis: ..." required /></label><div className="actions"><button type="button" className="secondary" disabled={!directTitle.trim() || directText.trim().length < 200} onClick={previewDirectScriptedVideo}>Preview structure</button><button className="primary" disabled={!activeChannelId || !directTitle.trim() || directText.trim().length < 200}>Import without evidence checking</button><span className="muted">{directText.trim().split(/\s+/).filter(Boolean).length} words · final render blocks unresolved placeholders</span></div>{directPreview && <div className="notice"><strong>{directPreview.scene_count} scenes · {Math.round(directPreview.total_duration_seconds)}s target · {directPreview.word_count} words</strong>{directPreview.placeholder_tokens.length ? <p>Unresolved placeholders: {directPreview.placeholder_tokens.join(", ")}</p> : <p>No unresolved placeholders detected.</p>}<div className="card-list compact-list">{directPreview.scenes.slice(0, 6).map(scene => <article className="profile-card subject" key={scene.scene_key}><div><strong>{scene.scene_key.toUpperCase()} · {scene.title}</strong><small>{Math.round(scene.duration_seconds)}s · {scene.word_count} words · {scene.on_screen_text.join(" / ") || "No on-screen text"}</small><small>{scene.placeholder_tokens.length ? `Placeholders: ${scene.placeholder_tokens.join(", ")}` : scene.assets.join(" · ") || "No asset request"}</small></div></article>)}</div>{directPreview.scenes.length > 6 && <p className="muted">Showing first 6 of {directPreview.scenes.length} parsed scenes.</p>}</div>}</form> : <p className="empty">Editor permission is required to import a direct scripted video.</p>}</details>
    <details className="panel action-disclosure"><summary><span><small>Use existing research</small><strong>Paste a script for evidence-bound processing</strong><em>Fast path for an Opportunity with an approved, explanation-ready source brief.</em></span><span className="chip">{importableDossiers.length ? `${importableDossiers.length} available` : "No eligible source brief"}</span></summary>{(role === "admin" || role === "editor") ? <form onSubmit={importExistingResearchScript}><label>Opportunity with completed source brief<select value={importOpportunityId} onChange={event => void selectImportOpportunity(event.target.value, importableDossiers)} required><option value="">Select opportunity</option>{importableDossiers.map(item => <option key={item.id} value={item.opportunity_id}>{opportunities.find(value => value.id === item.opportunity_id)?.title || item.executive_summary} · source brief v{item.dossier_version}</option>)}</select><small>The source brief must be approved and explanation-ready. Approved scripts, locked segments and existing storyboards are protected.</small></label><label>Script title<input value={importTitle} onChange={event => setImportTitle(event.target.value)} maxLength={300} required /></label><label>Paste complete script<textarea value={importText} onChange={event => setImportText(event.target.value)} minLength={200} maxLength={50000} rows={14} placeholder="Paste the complete narration here. Headings are optional; TubeFactory creates the production structure and binds supported statements to the selected research." required /></label><div className="actions"><button className="primary" disabled={!importOpportunityId || importText.trim().length < 200}>Import, ground & verify</button><span className="muted">{importText.trim().split(/\s+/).filter(Boolean).length} words · original input retained by hash</span></div><details><summary>AI drafting prompt for this source brief</summary><p className="muted">Select an Opportunity first. The template then includes its approved conclusions, limits, open questions and claims.</p><textarea readOnly rows={22} value={importPrompt} /><button type="button" className="secondary" onClick={() => navigator.clipboard.writeText(importPrompt)}>Copy AI prompt</button></details></form> : <p className="empty">Editor permission is required to import a script.</p>}{!importableDossiers.length && <div className="notice">No active Opportunity currently has an approved explanation-ready source brief that is safe to import into.</div>}</details>
    <section className="panel"><div className="panel-title"><div><p className="eyebrow">Automatic continuation</p><h2>Approved research inputs</h2></div></div><div className="card-list">{unstartedDossiers.map(item => <article className="profile-card subject" key={item.id}><div><strong>{item.executive_summary}</strong><small>Dossier v{item.dossier_version} · script generation starts with dossier approval</small></div><span className="status waiting">Automatically queued</span></article>)}{blockedApprovedDossiers.map(item => <article className="profile-card subject" key={item.id}><div><strong>{item.executive_summary}</strong><small>Script blocked: {(item.completion_evaluation.explanation_readiness?.gaps || ["legacy dossier must be enriched"]).join(" · ")}</small></div><span className="status waiting">Needs evidence</span></article>)}{!eligibleDossiers.length && !blockedApprovedDossiers.length ? <p className="empty">No approved dossier is ready in this channel. Complete research review first.</p> : !unstartedDossiers.length && !blockedApprovedDossiers.length ? <p className="empty">Every explanation-ready dossier in this channel already has a script.</p> : null}</div></section>
    <div className="research-grid"><section className="panel"><div className="panel-title"><div><p className="eyebrow">Versioned writing</p><h2>Scripts</h2></div><span className="chip">{scopedScripts.length}</span></div><div className="card-list">{scopedScripts.map(item => <button className="dossier-card" key={item.id} onClick={() => openScript(item.id)}><span><strong>{item.title}</strong><small>v{item.version} · {item.evidence_required ? `coverage ${item.coverage_percent}%` : "direct scripted video · no evidence check"} · {item.content_hash.slice(0, 12)}…</small></span><span className={`status ${item.status === "verified" || item.status === "approved" ? "good" : "waiting"}`}>{item.status}</span></button>)}{!scopedScripts.length && <p className="empty">No scripts belong to this channel yet.</p>}</div></section><section className="panel"><div className="panel-title"><div><p className="eyebrow">Strict SceneSpec</p><h2>Storyboards</h2></div><span className="chip">{scopedStoryboards.length}</span></div><div className="card-list">{scopedStoryboards.map(item => <button className="dossier-card" key={item.id} onClick={() => openStoryboard(item.id)}><span><strong>{scopedScripts.find(value => value.id === item.script_id)?.title || "Storyboard"}</strong><small>v{item.version} · {item.scene_count} scenes · {item.content_hash.slice(0, 12)}…</small></span><span className={`status ${item.status === "approved" ? "good" : "waiting"}`}>{item.status}</span></button>)}{scopedScripts.filter(item => item.status === "approved" && !scopedStoryboards.some(value => value.script_id === item.id)).map(item => <div className="notice" key={item.id}>Storyboard generation for {item.title} was started automatically by script approval.</div>)}{!scopedStoryboards.length && !scopedScripts.some(item => item.status === "approved") && <p className="empty">No storyboard is ready in this channel.</p>}</div></section></div>
    {script && edited && <section className="panel script-editor"><div className="panel-title"><div><p className="eyebrow">Script editor · immutable version {script.version}</p><h2>{script.title}</h2></div><div className="actions"><span className={`status ${script.status === "verified" || script.status === "approved" ? "good" : "waiting"}`}>{script.status}</span><button className="secondary compact" onClick={() => { setScript(null); setEdited(null); setDossier(null); setSelectedSegmentKeys([]); }}>Close</button></div></div><div className="verification-strip"><strong>{script.evidence_required ? `${script.coverage_percent}% central-claim coverage` : "Direct scripted video · evidence not applicable"}</strong><span>{script.evidence_required ? (script.verification_report.valid ? "Independent verification passed" : script.verification_report.deterministic_valid ? "Independent verification required" : "Policy warnings block approval") : "Operator-supplied content; reviewer owns factual approval"}</span><code>{script.content_hash}</code></div>{script.verification_report.issues?.length ? <div className="notice error">{script.verification_report.issues.map(item => <p key={`${item.code}-${item.segment_key}`}>{item.code}: {item.message}</p>)}</div> : null}<div className="script-columns"><div><h3>Narration and purpose</h3>{edited.segments.map((segment, index) => <article className={`script-segment ${segment.locked ? "locked" : ""}`} key={segment.id}><header><span className="chip">{segment.segment_order} · {segment.segment_type}</span><div className="actions"><label className="checkbox"><input type="checkbox" checked={selectedSegmentKeys.includes(segment.segment_key)} disabled={segment.locked || !script.evidence_required || !!changedSegments.length || !(role === "admin" || role === "editor")} onChange={event => setSelectedSegmentKeys(keys => event.target.checked ? [...keys, segment.segment_key] : keys.filter(key => key !== segment.segment_key))} /> regenerate</label><label className="checkbox"><input type="checkbox" checked={segment.locked} disabled={script.segments[index].locked || !(role === "admin" || role === "editor")} onChange={event => { const next = structuredClone(edited); next.segments[index].locked = event.target.checked; setEdited(next); setSelectedSegmentKeys(keys => keys.filter(key => key !== segment.segment_key)); }} /> locked</label></div></header><textarea value={segment.narration} disabled={segment.locked || !(role === "admin" || role === "editor")} onChange={event => editNarration(index, event.target.value)} rows={4} /><input value={segment.presentation_purpose} disabled={segment.locked || !(role === "admin" || role === "editor")} onChange={event => { const next = structuredClone(edited); next.segments[index].presentation_purpose = event.target.value; setEdited(next); }} /><div className="claim-badges">{segment.annotations.flatMap(item => item.claim_ids).map(id => <span className="chip" key={id}>{claims[id]?.claim_type || "claim"} · {id.slice(0, 8)}</span>)}{!segment.annotations.some(item => item.claim_ids.length) && <span className="chip">editorial / no factual claim</span>}</div></article>)}</div><aside className="evidence-drawer"><h3>Evidence drawer</h3>{!script.evidence_required && <p className="empty">Direct scripted-video mode: no dossier, claims, or evidence checking is attached. Reviewers approve factual and business correctness manually.</p>}{Object.values(claims).map(claim => <article key={claim.id}><span className="chip">{claim.claim_type} · {claim.status}</span><strong>{claim.normalized_statement}</strong>{claim.evidence.map((item, index) => <div key={`${claim.id}-${index}`}><blockquote>{item.exact_text}</blockquote><ExternalSourceLink url={item.source.canonical_url} title={item.source.title} /><small>{item.relationship} · {item.primary_source ? "primary" : "secondary"} · {item.source_independent ? "independent" : "dependent"}</small></div>)}</article>)}</aside></div><div className="review-bar"><label>Version / approval / regeneration instruction<input value={comment} onChange={event => setComment(event.target.value)} minLength={10} /></label><div><strong>Local diff</strong><p>{changedSegments.length ? `Changed: ${changedSegments.join(", ")}` : selectedSegmentKeys.length ? `Selected for regeneration: ${selectedSegmentKeys.join(", ")}` : "No unsaved segment changes."}</p></div><div className="actions">{(role === "admin" || role === "editor") && <button className="primary" disabled={!changedSegments.length} onClick={saveScript}>Save immutable version</button>}{(role === "admin" || role === "editor") && <button className="secondary" disabled={!script.evidence_required || !selectedSegmentKeys.length || !!changedSegments.length} onClick={regenerateSelection}>Regenerate selection</button>}{(role === "admin" || role === "operator") && script.status === "draft" && <button className="secondary" onClick={verifyEditedScript}>Run independent verifier</button>}{(role === "admin" || role === "reviewer") && script.status === "verified" && <button className="primary" onClick={approveScript}>Approve exact hash</button>}</div></div></section>}
    {storyboard && <section className="panel storyboard-editor"><div className="panel-title"><div><p className="eyebrow">Storyboard editor · immutable version {storyboard.version}</p><h2>{storyboard.scene_count} validated scenes</h2></div><div className="actions"><span className={`status ${storyboard.status === "approved" ? "good" : "waiting"}`}>{storyboard.status}</span><button className="secondary compact" onClick={() => { setStoryboard(null); setSelectedScene(null); setSceneAlternatives([]); }}>Close</button></div></div><div className="scene-grid">{storyboard.scenes.map(item => <article className={`scene-card ${item.locked ? "locked" : ""}`} key={item.id}><header><span className="chip">Scene {String(item.scene_spec.order)} · {String(item.scene_spec.visual_type)}</span><span>{Number(item.scene_spec.duration)}s</span></header><strong>{String(item.scene_spec.purpose)}</strong><p>{String(item.scene_spec.visual_brief)}</p><div className="claim-badges">{((item.scene_spec.claim_ids as string[] | undefined) || []).map(id => <span className="chip" key={id}>claim {id.slice(0, 8)}</span>)}</div><small>{item.content_hash.slice(0, 16)}… · scene v{item.version}</small>{(role === "admin" || role === "editor") && <div className="actions"><button className="secondary compact" onClick={() => setSceneLock(item, !item.locked)}>{item.locked ? "Unlock" : "Lock"}</button><button className="secondary compact" disabled={item.locked} onClick={() => openSceneEditor(item)}>Edit & alternatives</button><button className="secondary compact" disabled={item.locked} onClick={() => generateSceneAlternative(item)}>Regenerate</button></div>}</article>)}</div>{selectedScene && <div className="review-bar"><label>Strict SceneSpec JSON<textarea value={sceneJson} onChange={event => setSceneJson(event.target.value)} rows={18} /></label><div><strong>Generated alternatives</strong>{sceneAlternatives.length ? <div className="card-list">{sceneAlternatives.map(item => <article className="profile-card subject" key={item.id}><div><strong>Alternative {item.alternative_number}</strong><small>{String(item.scene_spec.visual_brief)} · {item.content_hash.slice(0, 12)}…</small><small>{item.current_base ? "Selectable for this scene version" : "Historical candidate"}</small></div><button className="secondary compact" disabled={!item.current_base} onClick={() => selectSceneAlternative(item)}>Select</button></article>)}</div> : <p className="empty">No generated alternatives for this scene version.</p>}</div><div className="actions"><button className="primary" onClick={saveScene}>Validate & create manual version</button><button className="secondary" onClick={() => { setSelectedScene(null); setSceneAlternatives([]); }}>Cancel</button></div></div>}<div className="review-bar"><label>Review / alternative instruction<input value={comment} onChange={event => setComment(event.target.value)} minLength={10} /></label>{(role === "admin" || role === "reviewer") && storyboard.status === "in_review" && <button className="primary" onClick={approveStoryboard}>Approve exact storyboard hash</button>}</div></section>}
  </>;
}

function MediaPanel({ csrf, role, activeChannelId, onOpenPage }: { csrf: string; role: Role; activeChannelId: string; onOpenPage: (page: string) => void }) {
  const [productions, setProductions] = useState<MediaProduction[]>([]);
  const [storyboards, setStoryboards] = useState<StoryboardSummary[]>([]);
  const [scripts, setScripts] = useState<ScriptSummary[]>([]);
  const [dossiers, setDossiers] = useState<Dossier[]>([]);
  const [opportunities, setOpportunities] = useState<Opportunity[]>([]);
  const [channelSubjectIds, setChannelSubjectIds] = useState<string[]>([]);
  const [workflows, setWorkflows] = useState<ComfyWorkflow[]>([]);
  const [voices, setVoices] = useState<VoiceProfile[]>([]);
  const [selected, setSelected] = useState<MediaProduction | null>(null);
  const [storyboardId, setStoryboardId] = useState("");
  const [workflowKey, setWorkflowKey] = useState("");
  const [voiceKey, setVoiceKey] = useState("");
  const [tier, setTier] = useState<"preview" | "full">("preview");
  const [run, setRun] = useState<EditorialRun | null>(null);
  const [comment, setComment] = useState("Reviewed against the exact approved storyboard, manifest and blocking QA report.");
  const [message, setMessage] = useState("");
  const [workflowJson, setWorkflowJson] = useState("{}");
  const [voiceJson, setVoiceJson] = useState("{}");
  const videoRef = useRef<HTMLVideoElement | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [nextProductions, nextStoryboards, nextWorkflows, nextVoices, nextScripts, nextDossiers, nextOpportunities, nextSubjects] = await Promise.all([
        api<MediaProduction[]>("/api/v1/media/productions"),
        api<StoryboardSummary[]>("/api/v1/editorial/storyboards"),
        api<ComfyWorkflow[]>("/api/v1/media/comfy-workflows"),
        api<VoiceProfile[]>("/api/v1/media/voice-profiles"),
        api<ScriptSummary[]>("/api/v1/editorial/scripts"),
        api<Dossier[]>("/api/v1/research/dossiers"),
        api<Opportunity[]>("/api/v1/research/opportunities"),
        api<SubjectProfile[]>("/api/v1/subject-profiles"),
      ]);
      const subjectIds = (activeChannelId ? nextSubjects.filter(item => item.channel_profile_id === activeChannelId) : nextSubjects).map(item => item.id);
      const opportunityIds = new Set(nextOpportunities.filter(item => subjectIds.includes(item.subject_profile_id)).map(item => item.id));
      const dossierIds = new Set(nextDossiers.filter(item => opportunityIds.has(item.opportunity_id)).map(item => item.id));
      const scriptIds = new Set(nextScripts.filter(item => item.channel_profile_id === activeChannelId || (item.dossier_id ? dossierIds.has(item.dossier_id) : false)).map(item => item.id));
      const visibleStoryboards = activeChannelId ? nextStoryboards.filter(item => scriptIds.has(item.script_id)) : nextStoryboards;
      const visibleVersionIds = new Set(visibleStoryboards.map(item => item.current_version_id));
      const visibleProductions = activeChannelId ? nextProductions.filter(item => visibleVersionIds.has(item.storyboard_version_id)) : nextProductions;
      setProductions(nextProductions); setStoryboards(nextStoryboards); setWorkflows(nextWorkflows); setVoices(nextVoices);
      setScripts(nextScripts); setDossiers(nextDossiers); setOpportunities(nextOpportunities); setChannelSubjectIds(subjectIds);
      setStoryboardId(current => visibleStoryboards.some(item => item.current_version_id === current) ? current : visibleStoryboards.find(item => item.status === "approved")?.current_version_id || "");
      setWorkflowKey(current => current || nextWorkflows.find(item => item.active && item.approval_state === "approved")?.workflow_key || "");
      setVoiceKey(current => current || nextVoices.find(item => item.active && item.enabled)?.profile_key || "");
      setSelected(current => current ? visibleProductions.find(item => item.id === current.id) || visibleProductions[0] || null : visibleProductions[0] || null);
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Media production data could not be loaded"); }
  }, [activeChannelId]);
  useEffect(() => { refresh(); }, [refresh]);
  useEffect(() => {
    if (!run || run.execution_status !== "RUNNING") return;
    const timer = window.setInterval(async () => {
      try {
        const next = await api<EditorialRun>(`/api/v1/editorial/runs/${encodeURIComponent(run.workflow_id)}`);
        setRun(next); await refresh();
      } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Render status could not be refreshed"); }
    }, 2500);
    return () => window.clearInterval(timer);
  }, [run, refresh]);

  async function startProduction(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const storyboard = storyboards.find(item => item.current_version_id === storyboardId);
    if (!storyboard) return;
    try {
      const full = tier === "full";
      const next = await api<EditorialRun>("/api/v1/media/productions", { method: "POST", body: JSON.stringify({ storyboard_version_id: storyboardId, expected_storyboard_hash: storyboard.content_hash, render_tier: tier, workflow_key: workflowKey, voice_profile_key: voiceKey, width: full ? 1920 : 854, height: full ? 1080 : 480, fps: full ? 24 : 12, idempotency_key: `ui-${randomUuid()}` }) }, csrf);
      setRun(next); setMessage(`${tier === "full" ? "Full-resolution" : "Low-resolution"} production started from exact storyboard hash ${storyboard.content_hash.slice(0, 12)}…`);
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Media production could not be started"); }
  }
  async function importRegistry(kind: "workflow" | "voice") {
    try {
      const body = kind === "workflow" ? workflowJson : voiceJson;
      await api(`/api/v1/media/${kind === "workflow" ? "comfy-workflows" : "voice-profiles"}`, { method: "POST", body: JSON.stringify(JSON.parse(body)) }, csrf);
      setMessage(`${kind === "workflow" ? "ComfyUI workflow" : "Voice profile"} version registered.`); await refresh();
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Registry payload is not valid JSON"); }
  }
  async function overrideFinding(finding: QAFinding) {
    try {
      const next = await api<MediaProduction>(`/api/v1/media/qa-findings/${finding.id}/override`, { method: "POST", body: JSON.stringify({ reason: comment }) }, csrf);
      setSelected(next); setMessage(`Audited override recorded for ${finding.code}.`); await refresh();
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "QA override failed"); }
  }
  async function regenerateSelection(kind: "scene" | "narration", id: string) {
    if (!selected) return;
    try {
      const path = kind === "scene" ? `scenes/${id}` : `narration/${id}`;
      const next = await api<EditorialRun>(`/api/v1/media/productions/${selected.id}/${path}/regenerate`, { method: "POST", body: JSON.stringify({ instruction: comment, idempotency_key: `ui-${randomUuid()}` }) }, csrf);
      setRun(next); setMessage(`${kind === "scene" ? "Scene alternative" : "Narration audition"} generation started without overwriting the approved render.`);
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Selected media regeneration failed"); }
  }
  async function reviewRender(decision: "approved" | "rejected") {
    if (!selected?.render || !selected.manifest) return;
    try {
      const next = await api<MediaProduction>(`/api/v1/media/renders/${selected.render.id}/approval`, { method: "POST", body: JSON.stringify({ expected_render_hash: selected.render.content_hash, expected_manifest_hash: selected.manifest.content_hash, decision, comment }) }, csrf);
      setSelected(next); setMessage(next.automatic_continuation?.message || `Exact render and manifest ${decision}.`); await refresh();
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Render review failed"); }
  }
  function seek(seconds: number) {
    if (!videoRef.current) return;
    videoRef.current.currentTime = Math.max(0, seconds); videoRef.current.play().catch(() => undefined);
  }

  const videoId = selected?.render?.video_asset_id;
  const captionId = selected?.assets.find(item => item.asset_kind === "caption_vtt")?.id;
  let cursor = 0;
  const sceneTimeline = (selected?.manifest?.document.scenes || []).map(scene => {
    const start = cursor; cursor += Number(scene.scene_spec.duration || 0); return { scene, start };
  });
  const sourceMap = new Map((selected?.manifest?.document.sources || []).map(source => [source.id, source]));
  const unresolvedFailures = selected?.findings.filter(item => item.verdict === "fail" && !item.overridden) || [];
  const auditionAssets = selected?.assets.filter(item => item.asset_kind === "narration_mastered" && item.generation_provenance.regeneration_workflow_id) || [];
  const canOperate = role === "admin" || role === "operator";
  const canReview = role === "admin" || role === "reviewer";
  const scopedOpportunityIds = new Set((activeChannelId ? opportunities.filter(item => channelSubjectIds.includes(item.subject_profile_id)) : opportunities).map(item => item.id));
  const scopedDossierIds = new Set((activeChannelId ? dossiers.filter(item => scopedOpportunityIds.has(item.opportunity_id)) : dossiers).map(item => item.id));
  const scopedScriptIds = new Set((activeChannelId ? scripts.filter(item => item.channel_profile_id === activeChannelId || (item.dossier_id ? scopedDossierIds.has(item.dossier_id) : false)) : scripts).map(item => item.id));
  const scopedStoryboards = activeChannelId ? storyboards.filter(item => scopedScriptIds.has(item.script_id)) : storyboards;
  const scopedStoryboardVersionIds = new Set(scopedStoryboards.map(item => item.current_version_id));
  const scopedProductions = activeChannelId ? productions.filter(item => scopedStoryboardVersionIds.has(item.storyboard_version_id)) : productions;
  const approvedStoryboards = scopedStoryboards.filter(item => item.status === "approved");
  const approvedWorkflows = workflows.filter(item => item.active && item.approval_state === "approved");
  const activeVoices = voices.filter(item => item.active && item.enabled);
  const renderBlockers = [
    approvedStoryboards.length ? "" : "Approve one storyboard in Scripts & storyboards.",
    approvedWorkflows.length ? "" : "Register and approve one ComfyUI visual workflow.",
    activeVoices.length ? "" : "Register one enabled voice profile.",
  ].filter(Boolean);
  const renderReady = renderBlockers.length === 0 && Boolean(storyboardId && workflowKey && voiceKey);
  const renderButtonLabel = !approvedStoryboards.length ? "Approve storyboard first" : !approvedWorkflows.length ? "Register workflow first" : !activeVoices.length ? "Register voice first" : !storyboardId ? "Select approved storyboard" : "Queue durable render";

  return <><div className="page-heading"><div><p className="eyebrow">Increment 3 media production</p><h1>Render and final review</h1></div><span className={`status ${scopedProductions.some(item => item.state === "ready") ? "good" : "waiting"}`}>{scopedProductions.length} immutable production{scopedProductions.length === 1 ? "" : "s"}</span></div>
    {message && <div className="notice">{message}</div>}
    {run && <section className="panel workflow-monitor"><div><span className={`status ${run.execution_status === "COMPLETED" ? "good" : run.execution_status === "FAILED" ? "bad" : "waiting"}`}>{run.state} · {run.progress}%</span><progress max="100" value={run.progress}>{run.progress}%</progress><code>{run.workflow_id}</code></div><div><strong>{run.execution_status}</strong><p className="muted">Temporal retains this job across worker and host restarts.</p></div><button className="secondary compact" onClick={refresh}>Refresh</button></section>}
    {canOperate && <section className="panel"><div className="panel-title"><div><p className="eyebrow">Exact approved input</p><h2>Start a render</h2></div><span className="chip">draft before master</span></div><div className={`readiness-card ${renderBlockers.length ? "" : "ready"}`}><div className="panel-title"><div><strong>{renderBlockers.length ? "Rendering is waiting for prerequisites" : "Ready to queue a durable render"}</strong><p className="muted">{renderBlockers.length ? "Fix the missing items below, then queue a low-resolution draft before producing a full master." : "The selected storyboard, visual workflow and voice profile are approved for this workspace."}</p></div><span className={`status ${renderBlockers.length ? "waiting" : "good"}`}>{renderBlockers.length ? `${renderBlockers.length} missing` : "ready"}</span></div><ul>{renderBlockers.length ? renderBlockers.map(item => <li key={item}>{item}</li>) : <li>Queue the preview render, inspect QA, then approve the exact render hash before Publishing can continue.</li>}</ul><div className="actions">{!approvedStoryboards.length && <button type="button" className="secondary compact" onClick={() => onOpenPage("editorial")}>Open scripts & storyboards</button>}{role === "admin" && (!approvedWorkflows.length || !activeVoices.length) && <a className="secondary compact" href="#media-provider-registries">Open media registries</a>}</div></div><form className="media-start" onSubmit={startProduction}><label>Approved storyboard<select value={storyboardId} onChange={event => setStoryboardId(event.target.value)} required disabled={!approvedStoryboards.length}><option value="">Select exact version</option>{approvedStoryboards.map(item => <option key={item.current_version_id} value={item.current_version_id}>v{item.version} · {item.content_hash.slice(0, 12)}…</option>)}</select></label><label>Approved visual workflow<select value={workflowKey} onChange={event => setWorkflowKey(event.target.value)} required disabled={!approvedWorkflows.length}><option value="">Select workflow</option>{approvedWorkflows.map(item => <option key={item.id} value={item.workflow_key}>{item.workflow_key} v{item.version_number}</option>)}</select></label><label>Voice profile<select value={voiceKey} onChange={event => setVoiceKey(event.target.value)} required disabled={!activeVoices.length}><option value="">Select voice</option>{activeVoices.map(item => <option key={item.id} value={item.profile_key}>{item.profile_key} · {item.engine}</option>)}</select></label><label>Tier<select value={tier} onChange={event => setTier(event.target.value as "preview" | "full")}><option value="preview">Low-resolution draft</option><option value="full">Full-resolution master</option></select></label><button className="primary" disabled={!renderReady}>{renderButtonLabel}</button></form></section>}
    {role === "admin" && <details id="media-provider-registries" className="panel registry-panel"><summary>Media provider registries</summary><p className="muted">Import only reviewed ComfyUI API-format workflows with allowlisted nodes/models. Voice profiles record engine settings and consent; secrets remain in the provider secret store.</p><div className="registry-grid"><label>ComfyUI workflow registry payload<textarea rows={10} value={workflowJson} onChange={event => setWorkflowJson(event.target.value)} spellCheck={false} /><button className="secondary" onClick={() => importRegistry("workflow")}>Validate and register version</button></label><label>Voice profile registry payload<textarea rows={10} value={voiceJson} onChange={event => setVoiceJson(event.target.value)} spellCheck={false} /><button className="secondary" onClick={() => importRegistry("voice")}>Register profile version</button></label></div></details>}
    <div className="media-layout"><section className="panel"><div className="panel-title"><div><p className="eyebrow">Render queue and history</p><h2>Productions</h2></div><span className="chip">{scopedProductions.length}</span></div><div className="card-list">{scopedProductions.map(item => <button className="dossier-card" key={item.id} onClick={() => setSelected(item)}><span><strong>{item.render_tier === "full" ? "Full master" : "Preview"} · {item.storyboard_hash.slice(0, 12)}…</strong><small>{new Date(item.created_at).toLocaleString()} · {item.render?.engine || "pending"}</small></span><span className={`status ${item.state === "ready" ? "good" : item.state === "blocked" ? "bad" : "waiting"}`}>{item.qa?.verdict || item.state}</span></button>)}{!scopedProductions.length && <p className="empty">No media production has been started for this channel.</p>}</div></section>
    <section className="panel media-player"><div className="panel-title"><div><p className="eyebrow">Exact render review</p><h2>{selected ? `${selected.render_tier} · ${selected.state}` : "Select a production"}</h2></div>{selected?.approval && <span className={`status ${selected.approval.decision === "approved" ? "good" : "bad"}`}>{selected.approval.decision}</span>}</div>{videoId ? <video ref={videoRef} controls preload="metadata" key={videoId}><source src={`/api/v1/media/assets/${videoId}/content`} type="video/mp4" />{captionId && <track default kind="captions" srcLang="en" label="English" src={`/api/v1/media/assets/${captionId}/content`} />}</video> : <p className="empty">The render has not completed yet.</p>}{selected?.render && <div className="hash-grid"><span>Render <code>{selected.render.content_hash}</code></span><span>Manifest <code>{selected.manifest?.content_hash}</code></span><span>Storyboard <code>{selected.storyboard_hash}</code></span></div>}</section></div>
    {selected?.manifest && <div className="review-layout"><section className="panel"><div className="panel-title"><div><p className="eyebrow">Timeline navigation</p><h2>Scenes, claims and sources</h2></div><span className="chip">{sceneTimeline.length} scenes</span></div><div className="timeline-list">{sceneTimeline.map(({ scene, start }) => <article key={scene.scene_version_id}><button onClick={() => seek(start)}><time>{Math.floor(start / 60)}:{String(Math.floor(start % 60)).padStart(2, "0")}</time><span><strong>Scene {scene.scene_spec.order || "—"} · {scene.scene_spec.purpose || "Untitled"}</strong><small>{scene.scene_spec.claim_ids?.length ? `Claims ${scene.scene_spec.claim_ids.map(id => id.slice(0, 8)).join(", ")}` : "No factual claim"}</small><small>{scene.scene_spec.source_ids?.map(id => sourceMap.get(id)?.title || id.slice(0, 8)).join(" · ") || "No scene source reference"}</small></span></button>{canOperate && <button className="secondary compact" onClick={() => regenerateSelection("scene", scene.scene_version_id)}>Generate visual alternative</button>}</article>)}</div><h3>Narration auditions</h3><div className="audition-list">{selected.manifest.document.narration?.map(item => <article key={item.script_segment_id}><span><strong>Segment {item.segment_order}</strong><small>{item.request.text || "Approved narration"}</small></span>{canOperate && <button className="secondary compact" onClick={() => regenerateSelection("narration", item.script_segment_id)}>Regenerate segment</button>}</article>)}{auditionAssets.map(item => <audio key={item.id} controls preload="metadata" src={`/api/v1/media/assets/${item.id}/content`} />)}</div></section>
    <section className="panel"><div className="panel-title"><div><p className="eyebrow">Blocking policy</p><h2>Automated QA</h2></div><span className={`status ${selected.qa?.verdict === "pass" ? "good" : selected.qa?.verdict === "warn" ? "waiting" : "bad"}`}>{selected.qa?.verdict || "pending"}</span></div><div className="qa-list">{selected.findings.map(item => <article key={item.id}><button onClick={() => seek(item.timecode_seconds || 0)}><span className={`status ${item.verdict === "pass" ? "good" : item.verdict === "warn" ? "waiting" : "bad"}`}>{item.verdict}</span><span><strong>{item.code.replaceAll("_", " ")}</strong><small>{item.message}</small></span></button>{item.overridden && <p>Override: {item.override_reason}</p>}{canReview && item.verdict === "fail" && !item.overridden && item.override_policy === "reasoned" && <button className="secondary compact" onClick={() => overrideFinding(item)}>Record reasoned override</button>}</article>)}</div>{canReview && selected.render && <div className="review-bar"><label>Review decision / override reason<input value={comment} onChange={event => setComment(event.target.value)} minLength={10} /></label><div><strong>{unresolvedFailures.length ? `${unresolvedFailures.length} blocking failure(s)` : "Approval gate clear"}</strong><p>{selected.approval?.comment || "No reviewer decision recorded."}</p></div><div className="actions"><button className="secondary" onClick={() => reviewRender("rejected")}>Reject</button><button className="primary" disabled={!!unresolvedFailures.length} onClick={() => reviewRender("approved")}>Approve exact hashes</button></div></div>}</section></div>}
  </>;
}

function PublishingPanel({ csrf, role, activeChannelId, onOpenPage }: { csrf: string; role: Role; activeChannelId: string; onOpenPage: (page: string) => void }) {
  const [config, setConfig] = useState<PublishingConfig | null>(null);
  const [channels, setChannels] = useState<ChannelProfile[]>([]);
  const [connections, setConnections] = useState<YouTubeConnection[]>([]);
  const [productions, setProductions] = useState<MediaProduction[]>([]);
  const [metadata, setMetadata] = useState<PublishMetadata[]>([]);
  const [uploads, setUploads] = useState<Publication[]>([]);
  const [storyboards, setStoryboards] = useState<StoryboardSummary[]>([]);
  const [scripts, setScripts] = useState<ScriptSummary[]>([]);
  const [dossiers, setDossiers] = useState<Dossier[]>([]);
  const [opportunities, setOpportunities] = useState<Opportunity[]>([]);
  const [channelSubjectIds, setChannelSubjectIds] = useState<string[]>([]);
  const [selectedRender, setSelectedRender] = useState("");
  const [selectedMetadata, setSelectedMetadata] = useState("");
  const [connectionId, setConnectionId] = useState("");
  const [title, setTitle] = useState("Evidence review: what the primary sources show");
  const [description, setDescription] = useState("A calm, evidence-first review of the available records, including uncertainty and counterevidence.");
  const [comment, setComment] = useState("Reviewed for private upload against the exact render and publishing metadata hashes.");
  const [mode, setMode] = useState<"dry_run" | "real">("dry_run");
  const [publishAt, setPublishAt] = useState("");
  const [message, setMessage] = useState("");

  const refresh = useCallback(async () => {
    try {
      const [nextConfig, nextChannels, nextConnections, nextProductions, nextMetadata, nextUploads, nextStoryboards, nextScripts, nextDossiers, nextOpportunities, nextSubjects] = await Promise.all([
        api<PublishingConfig>("/api/v1/publishing/configuration"),
        api<ChannelProfile[]>("/api/v1/channel-profiles"),
        api<YouTubeConnection[]>("/api/v1/publishing/connections"),
        api<MediaProduction[]>("/api/v1/media/productions"),
        api<PublishMetadata[]>("/api/v1/publishing/metadata"),
        api<Publication[]>("/api/v1/publishing/uploads"),
        api<StoryboardSummary[]>("/api/v1/editorial/storyboards"),
        api<ScriptSummary[]>("/api/v1/editorial/scripts"),
        api<Dossier[]>("/api/v1/research/dossiers"),
        api<Opportunity[]>("/api/v1/research/opportunities"),
        api<SubjectProfile[]>("/api/v1/subject-profiles"),
      ]);
      setConfig(nextConfig); setChannels(nextChannels); setConnections(nextConnections);
      setProductions(nextProductions); setMetadata(nextMetadata); setUploads(nextUploads); setStoryboards(nextStoryboards); setScripts(nextScripts); setDossiers(nextDossiers); setOpportunities(nextOpportunities);
      const subjectIds = (activeChannelId ? nextSubjects.filter(item => item.channel_profile_id === activeChannelId) : nextSubjects).map(item => item.id);
      const opportunityIds = new Set(nextOpportunities.filter(item => subjectIds.includes(item.subject_profile_id)).map(item => item.id));
      const dossierIds = new Set(nextDossiers.filter(item => opportunityIds.has(item.opportunity_id)).map(item => item.id));
      const scriptIds = new Set(nextScripts.filter(item => item.channel_profile_id === activeChannelId || (item.dossier_id ? dossierIds.has(item.dossier_id) : false)).map(item => item.id));
      const storyboardVersionIds = new Set(nextStoryboards.filter(item => !activeChannelId || scriptIds.has(item.script_id)).map(item => item.current_version_id));
      const visibleProductions = activeChannelId ? nextProductions.filter(item => storyboardVersionIds.has(item.storyboard_version_id)) : nextProductions;
      setChannelSubjectIds(subjectIds);
      const eligible = visibleProductions.find(item => item.render && item.manifest && item.approval?.decision === "approved");
      setSelectedRender(current => visibleProductions.some(item => item.render?.id === current) ? current : eligible?.render?.id || "");
      const visibleConnections = activeChannelId ? nextConnections.filter(item => item.channel_profile_id === activeChannelId) : nextConnections;
      setConnectionId(current => visibleConnections.some(item => item.id === current) ? current : visibleConnections.find(item => item.enabled && item.status === "active")?.id || "");
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Publishing state could not be loaded"); }
  }, [activeChannelId]);
  useEffect(() => { refresh(); }, [refresh]);

  const scopedOpportunityIds = new Set((activeChannelId ? opportunities.filter(item => channelSubjectIds.includes(item.subject_profile_id)) : opportunities).map(item => item.id));
  const scopedDossierIds = new Set((activeChannelId ? dossiers.filter(item => scopedOpportunityIds.has(item.opportunity_id)) : dossiers).map(item => item.id));
  const scopedScriptIds = new Set((activeChannelId ? scripts.filter(item => item.channel_profile_id === activeChannelId || (item.dossier_id ? scopedDossierIds.has(item.dossier_id) : false)) : scripts).map(item => item.id));
  const scopedStoryboardVersionIds = new Set((activeChannelId ? storyboards.filter(item => scopedScriptIds.has(item.script_id)) : storyboards).map(item => item.current_version_id));
  const scopedProductions = activeChannelId ? productions.filter(item => scopedStoryboardVersionIds.has(item.storyboard_version_id)) : productions;
  const scopedRenderIds = new Set(scopedProductions.flatMap(item => item.render ? [item.render.id] : []));
  const scopedMetadata = activeChannelId ? metadata.filter(item => scopedRenderIds.has(item.render_id)) : metadata;
  const production = scopedProductions.find(item => item.render?.id === selectedRender);
  const selectedVersion = scopedMetadata.find(item => item.id === selectedMetadata);
  const caption = production?.assets.find(item => item.asset_kind === "caption_vtt" || item.asset_kind === "caption_srt");
  const thumbnail = production?.assets.find(item => item.asset_kind === "thumbnail");
  const canEdit = role === "admin" || role === "editor";
  const canReview = role === "admin" || role === "reviewer";
  const canUpload = role === "admin" || role === "operator";
  useEffect(() => {
    const eligible = scopedMetadata.filter(item => item.render_id === selectedRender);
    setSelectedMetadata(current => eligible.some(item => item.id === current) ? current : eligible[0]?.id || "");
  }, [selectedRender, scopedMetadata.map(item => item.id).join("|")]);

  async function configure(real: boolean) {
    try {
      await api("/api/v1/publishing/configuration", { method: "POST", body: JSON.stringify({ real_uploads_enabled: real, provider: "youtube", comment: real ? "Administrator explicitly enabled real private uploads." : "Administrator set publishing to dry-run only." }) }, csrf);
      setMessage(real ? "Real private uploads explicitly enabled. Public release still requires a separate approval." : "Publishing is dry-run only."); await refresh();
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Publishing configuration failed"); }
  }
  async function connectMock() {
    const channel = (activeChannelId && channels.find(item => item.id === activeChannelId)) || channels[0]; if (!channel) return;
    try {
      await api("/api/v1/publishing/connections/mock", { method: "POST", body: JSON.stringify({ channel_profile_id: channel.id, youtube_channel_id: `mock_${channel.slug}`, youtube_channel_title: `${channel.name} mock channel` }) }, csrf);
      setMessage("Development mock channel connected with encrypted fixture credentials."); await refresh();
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Mock connection failed"); }
  }
  async function startOAuth() {
    const channel = (activeChannelId && channels.find(item => item.id === activeChannelId)) || channels[0]; if (!channel) return;
    try {
      const redirectUri = `${window.location.origin}/api/v1/publishing/oauth/callback`;
      const next = await api<{ authorization_url: string }>("/api/v1/publishing/oauth/start", { method: "POST", body: JSON.stringify({ channel_profile_id: channel.id, redirect_uri: redirectUri }) }, csrf);
      window.location.assign(next.authorization_url);
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "OAuth authorization could not start"); }
  }
  async function createMetadata() {
    if (!production?.render || !production.manifest || !caption || !thumbnail) return;
    const sources = (production.manifest.document.sources || []).map(item => ({ title: item.title, url: item.url }));
    const chapters = (production.manifest.document.chapters || []).map((item, index) => ({ title: item.title || `Chapter ${index + 1}`, start_seconds: Math.floor(Number(item.start_seconds ?? item.timecode_seconds ?? 0)) }));
    try {
      const next = await api<PublishMetadata>("/api/v1/publishing/metadata", { method: "POST", body: JSON.stringify({
        render_id: production.render.id, expected_render_hash: production.render.content_hash, title, description,
        sources, evidence_url: null, chapters, tags: ["evidence", "sources", "fact check"], category_id: "27", language: "en",
        made_for_kids: false, contains_synthetic_media: production.manifest.document.scenes?.some(item => Boolean((item.scene_spec as Record<string, unknown>).synthetic_media_flag)) || false,
        captions: { asset_id: caption.id, language: "en", name: "English" }, thumbnail: { asset_id: thumbnail.id }, comment,
      }) }, csrf);
      setSelectedMetadata(next.id); setMessage(`Immutable metadata v${next.version_number} created: ${next.content_hash.slice(0, 12)}…`); await refresh();
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Metadata version could not be created"); }
  }
  async function approve(purpose: "private_upload" | "public_release", upload?: Publication) {
    const boundProduction = upload ? productions.find(item => item.render?.id === upload.render_id) : production;
    const boundMetadata = upload ? metadata.find(item => item.id === upload.metadata_version_id) : selectedVersion;
    if (!boundProduction?.render || !boundMetadata) return;
    try {
      const result = await api<{ automatic_continuation?: AutomaticContinuation | null }>("/api/v1/publishing/approvals", { method: "POST", body: JSON.stringify({
        purpose, render_id: boundProduction.render.id, expected_render_hash: boundProduction.render.content_hash,
        metadata_version_id: boundMetadata.id, expected_metadata_hash: boundMetadata.content_hash,
        decision: "approved", comment,
        connection_id: purpose === "private_upload" ? connectionId : null,
        mode: purpose === "private_upload" ? mode : null,
        publish_at: purpose === "public_release" && publishAt ? new Date(publishAt).toISOString() : null,
      }) }, csrf);
      setMessage(result.automatic_continuation?.message || `${purpose === "private_upload" ? "Private upload" : "Public release"} approved for the exact render and metadata hashes.`); await refresh();
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Publication approval failed"); }
  }
  async function reconcile(item: Publication) {
    try { await api(`/api/v1/publishing/uploads/${item.id}/reconcile`, { method: "POST" }, csrf); setMessage("Processing reconciliation queued."); }
    catch (caught) { setMessage(caught instanceof Error ? caught.message : "Reconciliation failed"); }
  }

  const scopedChannels = activeChannelId ? channels.filter(item => item.id === activeChannelId) : channels;
  const scopedConnections = activeChannelId ? connections.filter(item => item.channel_profile_id === activeChannelId) : connections;
  const scopedConnectionIds = new Set(scopedConnections.map(item => item.id));
  const scopedUploads = activeChannelId ? uploads.filter(item => scopedConnectionIds.has(item.connection_id)) : uploads;
  const mismatchedUploadIds = new Set(activeChannelId ? scopedUploads.filter(item => !scopedRenderIds.has(item.render_id)).map(item => item.id) : []);
  const approvedRenderProductions = scopedProductions.filter(item => item.render && item.manifest && item.approval?.decision === "approved");
  const connectedUploadTargets = scopedConnections.filter(item => item.enabled && item.status === "active");
  const metadataBlockers = [
    approvedRenderProductions.length ? "" : "Approve one final render in Media & final review.",
    production && !caption ? "The selected render needs captions." : "",
    production && !thumbnail ? "The selected render needs a thumbnail." : "",
  ].filter(Boolean);
  const uploadBlockers = [
    selectedVersion ? "" : "Create or select one immutable metadata version.",
    connectedUploadTargets.length ? "" : "Connect an active YouTube channel for this workspace.",
    connectionId ? "" : "Select the publishing channel.",
  ].filter(Boolean);
  const metadataButtonLabel = !production?.render ? "Select approved render first" : !caption || !thumbnail ? "Media assets incomplete" : "Create exact metadata version";
  return <><div className="page-heading"><div><p className="eyebrow">Increment 4 publishing</p><h1>Private upload and release calendar</h1></div><span className={`status ${config?.real_uploads_enabled ? "waiting" : "good"}`}>{config?.real_uploads_enabled ? "real private upload enabled" : "dry-run only"}</span></div>
    {message && <div className="notice">{message}</div>}
    {role === "admin" && <section className="panel"><div className="panel-title"><div><p className="eyebrow">Administrative safety gate</p><h2>Publishing mode and channel OAuth</h2></div><span className="chip">public release is always separate</span></div><p className="muted">Enabling real mode permits only resumable private uploads. It does not authorize scheduling or public release.</p>{!activeChannelId && <div className="notice">Choose one channel in the workspace selector before connecting a publishing account.</div>}<div className="actions"><button className="secondary" onClick={() => configure(false)}>Force dry-run</button><button className="secondary" onClick={() => configure(true)}>Enable real private uploads</button><button className="secondary" disabled={!activeChannelId || !scopedChannels.length} onClick={startOAuth}>Connect this channel with OAuth</button><button className="secondary" disabled={!activeChannelId || !scopedChannels.length} onClick={connectMock}>Connect this channel to mock</button></div></section>}
    <div className="media-layout publishing-layout"><section className="panel"><div className="panel-title"><div><p className="eyebrow">Immutable release material</p><h2>Metadata version</h2></div><span className="chip">{scopedMetadata.length} version{scopedMetadata.length === 1 ? "" : "s"}</span></div><div className={`readiness-card ${metadataBlockers.length ? "" : "ready"}`}><div className="panel-title"><div><strong>{metadataBlockers.length ? "Metadata is waiting for media approval" : "Ready to create publishing metadata"}</strong><p className="muted">{metadataBlockers.length ? "Publishing starts only from an approved render with captions and thumbnail assets." : "Create an immutable metadata version from the exact render, then approve private upload separately."}</p></div><span className={`status ${metadataBlockers.length ? "waiting" : "good"}`}>{metadataBlockers.length ? `${metadataBlockers.length} missing` : "ready"}</span></div><ul>{metadataBlockers.length ? metadataBlockers.map(item => <li key={item}>{item}</li>) : <li>Review the title, description, sources, chapters, caption and thumbnail binding before creating the metadata hash.</li>}</ul><div className="actions">{!approvedRenderProductions.length && <button type="button" className="secondary compact" onClick={() => onOpenPage("media")}>Open media review</button>}</div></div><label>Exact approved render<select value={selectedRender} onChange={event => { setSelectedRender(event.target.value); setSelectedMetadata(""); }} disabled={!approvedRenderProductions.length}><option value="">Select render</option>{approvedRenderProductions.map(item => <option key={item.render!.id} value={item.render!.id}>{item.render!.tier} · {item.render!.content_hash.slice(0, 12)}…</option>)}</select></label>{!approvedRenderProductions.length && <div className="notice">No approved render belongs to this channel. Complete media review first.</div>}<label>Title<input value={title} maxLength={100} onChange={event => setTitle(event.target.value)} disabled={!production?.render} /></label><label>Description<textarea rows={5} value={description} onChange={event => setDescription(event.target.value)} disabled={!production?.render} /></label><label>Review comment<input value={comment} minLength={10} onChange={event => setComment(event.target.value)} disabled={!production?.render} /></label>{production && (!caption || !thumbnail) && <div className="notice error">This render is missing {![caption, thumbnail].filter(Boolean).length ? "captions and thumbnail" : !caption ? "captions" : "a thumbnail"}; publishing metadata remains blocked.</div>}{canEdit && <button className="primary" disabled={!production?.render || !caption || !thumbnail} onClick={createMetadata}>{metadataButtonLabel}</button>}</section>
    <section className="panel"><div className="panel-title"><div><p className="eyebrow">Approval-bound upload</p><h2>Private first</h2></div><span className="chip">{connectedUploadTargets.length} active channel{connectedUploadTargets.length === 1 ? "" : "s"}</span></div><div className={`readiness-card ${uploadBlockers.length ? "" : "ready"}`}><div className="panel-title"><div><strong>{uploadBlockers.length ? "Private upload is waiting" : "Ready for private upload approval"}</strong><p className="muted">{uploadBlockers.length ? "A reviewer approves the exact metadata hash and render hash before any upload job is queued." : "Approve once for private upload; public release still requires a separate approval later."}</p></div><span className={`status ${uploadBlockers.length ? "waiting" : "good"}`}>{uploadBlockers.length ? `${uploadBlockers.length} missing` : "ready"}</span></div><ul>{uploadBlockers.length ? uploadBlockers.map(item => <li key={item}>{item}</li>) : <li>Confirm the selected connection and execution mode, then approve the private upload queue entry.</li>}</ul></div><label>Metadata version<select value={selectedMetadata} onChange={event => setSelectedMetadata(event.target.value)} disabled={!scopedMetadata.some(item => item.render_id === selectedRender)}><option value="">Select metadata</option>{scopedMetadata.filter(item => item.render_id === selectedRender).map(item => <option key={item.id} value={item.id}>v{item.version_number} · {item.content_hash.slice(0, 12)}…</option>)}</select></label><label>YouTube channel<select value={connectionId} onChange={event => setConnectionId(event.target.value)} disabled={!connectedUploadTargets.length}><option value="">Select channel</option>{connectedUploadTargets.map(item => <option key={item.id} value={item.id}>{item.youtube_channel_title}</option>)}</select></label><label>Upload execution<select value={mode} onChange={event => setMode(event.target.value as "dry_run" | "real")}><option value="dry_run">Dry-run / mock provider</option><option value="real" disabled={!config?.real_uploads_enabled}>Real resumable private upload</option></select></label>{selectedVersion && <div className="hash-grid"><span>Render <code>{production?.render?.content_hash}</code></span><span>Metadata <code>{selectedVersion.content_hash}</code></span></div>}<div className="actions">{canReview && <button className="primary" disabled={!selectedVersion || !connectionId} onClick={() => approve("private_upload")}>{selectedVersion && connectionId ? "Approve & queue private upload" : "Complete upload prerequisites"}</button>}</div></section></div>
    <section className="panel"><div className="panel-title"><div><p className="eyebrow">Processing and publication calendar</p><h2>Upload history</h2></div><button className="secondary compact" onClick={refresh}>Refresh</button></div>{mismatchedUploadIds.size > 0 && <div className="notice error">{mismatchedUploadIds.size} historical upload{mismatchedUploadIds.size === 1 ? " references" : "s reference"} a render owned by another channel. Follow-on publishing actions are blocked until the lineage is corrected.</div>}<label>Schedule time (local)<input type="datetime-local" value={publishAt} onChange={event => setPublishAt(event.target.value)} /></label><div className="card-list">{scopedUploads.map(item => <article className={`profile-card subject ${mismatchedUploadIds.has(item.id) ? "lineage-mismatch" : ""}`} key={item.id}><div><strong>{item.mode} · {item.state}</strong><small>{item.youtube_video_id || "awaiting provider video ID"} · {new Date(item.created_at).toLocaleString()}</small><small>Render {item.render_hash.slice(0, 12)}… · metadata {item.metadata_hash.slice(0, 12)}…</small><small>Captions {String(item.caption_status.status || "pending")} · thumbnail {String(item.thumbnail_status.status || "pending")}</small>{mismatchedUploadIds.has(item.id) && <small className="danger">Channel lineage mismatch · actions blocked</small>}{item.failure && <small>{JSON.stringify(item.failure)}</small>}</div><div className="actions">{canUpload && item.youtube_video_id && <button className="secondary compact" disabled={mismatchedUploadIds.has(item.id)} onClick={() => reconcile(item)}>Reconcile processing</button>}{canReview && item.mode === "real" && item.youtube_video_id && <button className="secondary compact" disabled={!publishAt || mismatchedUploadIds.has(item.id)} onClick={() => approve("public_release", item)}>Approve release & schedule</button>}</div></article>)}{!scopedUploads.length && <p className="empty">No private upload has been queued for this channel.</p>}</div></section>
  </>;
}

function OperationsPanel({ csrf, role, activeChannelId }: { csrf: string; role: Role; activeChannelId: string }) {
  const [analytics, setAnalytics] = useState<AnalyticsDashboard | null>(null);
  const [recommendations, setRecommendations] = useState<ModelRecommendation[]>([]);
  const [channels, setChannels] = useState<ChannelProfile[]>([]);
  const [models, setModels] = useState<AIModel[]>([]);
  const [assignments, setAssignments] = useState<TaskAssignment[]>([]);
  const [sources, setSources] = useState<SourceBrowserItem[]>([]);
  const [scripts, setScripts] = useState<ScriptSummary[]>([]);
  const [publications, setPublications] = useState<Publication[]>([]);
  const [operationalEvidence, setOperationalEvidence] = useState<OperationalEvidence[]>([]);
  const [message, setMessage] = useState("");
  const [taskType, setTaskType] = useState("script_writer");
  const [firstModel, setFirstModel] = useState("");
  const [secondModel, setSecondModel] = useState("");
  const [correctionTargetType, setCorrectionTargetType] = useState<"source" | "script" | "publication">("source");
  const canAdmin = role === "admin";
  const canOperate = role === "admin" || role === "operator";
  const canReview = role === "admin" || role === "reviewer";

  const refresh = useCallback(async () => {
    try {
      const [nextAnalytics, nextRecommendations, nextChannels, nextSources, nextScripts, nextPublications, nextEvidence] = await Promise.all([
        api<AnalyticsDashboard>("/api/v1/optimization/analytics/dashboard"),
        api<ModelRecommendation[]>("/api/v1/optimization/recommendations"),
        api<ChannelProfile[]>("/api/v1/channel-profiles"),
        api<SourceBrowserItem[]>("/api/v1/research/sources"),
        api<ScriptSummary[]>("/api/v1/editorial/scripts"),
        api<Publication[]>("/api/v1/publishing/uploads"),
        api<OperationalEvidence[]>("/api/v1/optimization/operational-evidence"),
      ]);
      setAnalytics(nextAnalytics); setRecommendations(nextRecommendations); setChannels(nextChannels);
      setSources(nextSources); setScripts(nextScripts); setPublications(nextPublications); setOperationalEvidence(nextEvidence);
      if (canAdmin) {
        const [nextModels, nextAssignments] = await Promise.all([api<AIModel[]>("/api/v1/models"), api<TaskAssignment[]>("/api/v1/task-model-assignments")]);
        setModels(nextModels); setAssignments(nextAssignments);
        const eligible = nextModels.filter(item => item.enabled && item.visible);
        setFirstModel(current => current || eligible[0]?.id || ""); setSecondModel(current => current || eligible[1]?.id || "");
      }
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Optimization and operations state could not be loaded"); }
  }, [canAdmin]);
  useEffect(() => { refresh(); }, [refresh]);

  async function ingestAnalytics(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const data = new FormData(event.currentTarget); const end = new Date(); const start = new Date(end.getTime() - 86400000);
    try {
      await api("/api/v1/optimization/analytics/snapshots", { method: "POST", body: JSON.stringify({
        channel_profile_id: data.get("channel_profile_id"), publication_id: null,
        youtube_video_id: data.get("youtube_video_id"), period_start: start.toISOString(), period_end: end.toISOString(),
        metrics: { views: Number(data.get("views")), watch_time_minutes: Number(data.get("watch_time_minutes")), subscribers_gained: Number(data.get("subscribers_gained")) },
        dimensions: { source: "operator_import" }, source: "operator_import",
      }) }, csrf);
      setMessage("Immutable analytics snapshot ingested with content-hash provenance."); await refresh();
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Analytics ingestion failed"); }
  }
  async function benchmark(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const data = new FormData(event.currentTarget);
    try {
      await api("/api/v1/optimization/benchmarks", { method: "POST", body: JSON.stringify({
        task_type: taskType, suite_key: "operator-controlled-benchmark", suite_version: String(data.get("suite_version")),
        candidates: [
          { model_id: firstModel, quality_score: Number(data.get("quality_one")), success_rate: Number(data.get("success_one")), p95_latency_ms: Number(data.get("latency_one")), mean_cost_usd: String(data.get("cost_one")), policy_eligible: true },
          { model_id: secondModel, quality_score: Number(data.get("quality_two")), success_rate: Number(data.get("success_two")), p95_latency_ms: Number(data.get("latency_two")), mean_cost_usd: String(data.get("cost_two")), policy_eligible: true },
        ], weights: { quality: 0.65, reliability: 0.20, latency: 0.10, cost: 0.05 },
      }) }, csrf);
      setMessage("Benchmark evidence stored and recommendation created. Routing remains unchanged until human approval, which applies the reviewed route automatically."); await refresh();
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Benchmark failed"); }
  }
  async function decide(item: ModelRecommendation, decision: "approved" | "rejected") {
    try {
      const result = await api<ModelRecommendation>(`/api/v1/optimization/recommendations/${item.id}/decision`, { method: "POST", body: JSON.stringify({ decision, reason: `Human ${decision} after reviewing the benchmark candidates, weights, score, and immutable baseline.` }) }, csrf);
      setMessage(result.applied ? "Recommendation approved and applied automatically as a new immutable routing version." : `Recommendation ${decision}.`); await refresh();
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Recommendation review failed"); }
  }
  async function freshness(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const data = new FormData(event.currentTarget);
    try { const result = await api<{ verdict: string; reason: string }>("/api/v1/optimization/freshness-checks", { method: "POST", body: JSON.stringify({ source_snapshot_id: data.get("snapshot_id"), maximum_age_seconds: Number(data.get("maximum_age_days")) * 86400 }) }, csrf); setMessage(`Freshness ${result.verdict}: ${result.reason}.`); }
    catch (caught) { setMessage(caught instanceof Error ? caught.message : "Freshness check failed"); }
  }
  async function originality(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const data = new FormData(event.currentTarget); const sourceKey = String(data.get("comparison_key"));
    try { const result = await api<{ verdict: string; maximum_overlap: number }>("/api/v1/optimization/originality-reports", { method: "POST", body: JSON.stringify({ script_version_id: data.get("script_version_id"), comparisons: { [sourceKey]: Number(data.get("overlap")) }, review_threshold: 0.20, block_threshold: 0.35 }) }, csrf); setMessage(`Originality ${result.verdict}; maximum overlap ${(result.maximum_overlap * 100).toFixed(1)}%.`); }
    catch (caught) { setMessage(caught instanceof Error ? caught.message : "Originality report failed"); }
  }
  async function correction(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const data = new FormData(event.currentTarget); const targetType = String(data.get("target_type"));
    try { const result = await api<{ required_action: string; affected_claims: unknown[]; affected_timecodes: unknown[] }>("/api/v1/optimization/corrections", { method: "POST", body: JSON.stringify({ publication_id: targetType === "publication" ? data.get("target_id") : null, script_version_id: targetType === "script" ? data.get("target_id") : null, source_snapshot_id: targetType === "source" ? data.get("target_id") : null, change_kind: data.get("change_kind"), severity: data.get("severity"), finding: data.get("finding"), evidence: { recorded_from: "operations_ui" } }) }, csrf); setMessage(`Correction recorded; required action: ${result.required_action}; ${result.affected_claims.length} affected claim(s), ${result.affected_timecodes.length} timecode(s).`); }
    catch (caught) { setMessage(caught instanceof Error ? caught.message : "Correction could not be recorded"); }
  }
  async function budgetPolicy(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const data = new FormData(event.currentTarget); const scope = String(data.get("scope"));
    try { await api(`/api/v1/optimization/budgets/${encodeURIComponent(scope)}`, { method: "PUT", body: JSON.stringify({ scope, currency: "USD", limit_amount: String(data.get("limit_amount")), period: data.get("period"), comment: "Administrator activated this budget through the operations UI." }) }, csrf); setMessage("Versioned budget policy activated."); }
    catch (caught) { setMessage(caught instanceof Error ? caught.message : "Budget policy failed"); }
  }
  async function budgetUsage(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const data = new FormData(event.currentTarget);
    try { await api("/api/v1/optimization/budget-usage", { method: "POST", body: JSON.stringify({ scope: data.get("scope"), idempotency_key: `ui-${randomUuid()}`, amount: String(data.get("amount")), currency: "USD", category: data.get("category"), workflow_id: null, details: { recorded_from: "operations_ui" } }) }, csrf); setMessage("Budget usage accepted and appended."); }
    catch (caught) { setMessage(caught instanceof Error ? caught.message : "Budget usage was blocked"); }
  }
  async function recordEvidence(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const data = new FormData(event.currentTarget); const artifactHash = String(data.get("artifact_hash") || "").trim();
    try { await api("/api/v1/optimization/operational-evidence", { method: "POST", body: JSON.stringify({ evidence_kind: data.get("evidence_kind"), document: JSON.parse(String(data.get("document"))), artifact_hash: artifactHash || null }) }, csrf); setMessage("Immutable operational evidence registered with canonical content hash."); await refresh(); }
    catch (caught) { setMessage(caught instanceof Error ? caught.message : "Operational evidence could not be registered"); }
  }

  const snapshots = sources.flatMap(source => source.snapshots.map(snapshot => ({ ...snapshot, sourceTitle: source.title })));
  const activeAssignments = Object.fromEntries(assignments.filter(item => item.active).map(item => [item.task_type, item]));
  const totals = Object.entries(analytics?.totals || {});
  const scopedChannels = activeChannelId ? channels.filter(item => item.id === activeChannelId) : channels;
  const pendingRecommendations = recommendations.filter(item => item.decision === "pending" && !item.applied).length;
  return <><div className="page-heading"><div><p className="eyebrow">Increment 5 optimization and hardening</p><h1>Analytics, governance and operations</h1><p className="muted">Use this page for measured performance, audited operational evidence and low-frequency governance tools.</p></div><a className="secondary" href="/grafana/" target="_blank" rel="noopener noreferrer">Open observability</a></div>
    {message && <div className="notice">{message}</div>}
    <div className="stat-grid"><article><span>Metric snapshots</span><strong>{analytics?.snapshot_count || 0}</strong><small>{analytics?.video_count || 0} videos · immutable imports</small></article><article><span>Recommendations</span><strong>{pendingRecommendations}</strong><small>{recommendations.length} total route review{recommendations.length === 1 ? "" : "s"}</small></article><article><span>Evidence inputs</span><strong>{snapshots.length}</strong><small>{scripts.length} script version{scripts.length === 1 ? "" : "s"} available for gates</small></article><article><span>Operations evidence</span><strong>{operationalEvidence.length}</strong><small>Backup, restore, SBOM and security records</small></article>{totals.slice(0, 2).map(([name, value]) => <article key={name}><span>{name.replaceAll("_", " ")}</span><strong>{value.toLocaleString()}</strong><small>Across the current dashboard window</small></article>)}</div>
    <section className="panel operations-guide"><div className="panel-title"><div><p className="eyebrow">Choose an operations task</p><h2>Most work here is periodic, not daily production</h2></div></div><div className="operations-task-grid"><article><strong>Record daily performance</strong><p>Import measured views, watch time and subscribers for a channel.</p><span className="status good">visible below</span></article><article><strong>Check recoverability</strong><p>Review backup, restore drill, SBOM and observability commands.</p><span className="status good">visible below</span></article><article><strong>Review model routing</strong><p>Approve recommendations only after benchmark evidence exists.</p><span className={pendingRecommendations ? "status waiting" : "status good"}>{pendingRecommendations ? `${pendingRecommendations} pending` : "none pending"}</span></article><article><strong>Use advanced tools</strong><p>Open expandable sections for evidence registry, budgets, freshness, originality and corrections.</p><span className="chip">collapsed by default</span></article></div></section>
    <div className="split-grid"><section className="panel"><div className="panel-title"><div><p className="eyebrow">Append-only performance evidence</p><h2>Analytics ingestion</h2></div><span className="chip">{analytics?.provenance.length || 0} hashes</span></div>{canOperate ? <form onSubmit={ingestAnalytics}><label>Channel<select name="channel_profile_id" required>{scopedChannels.map(item => <option value={item.id} key={item.id}>{item.name}</option>)}</select></label><label>YouTube video ID<input name="youtube_video_id" defaultValue="fixture_metrics_video" pattern="[A-Za-z0-9_-]+" required /></label><div className="form-pair"><label>Views<input name="views" type="number" min="0" defaultValue="1250" /></label><label>Watch minutes<input name="watch_time_minutes" type="number" min="0" defaultValue="420" /></label></div><label>Subscribers gained<input name="subscribers_gained" type="number" min="0" defaultValue="12" /></label><button className="primary" disabled={!scopedChannels.length}>Ingest daily snapshot</button></form> : <p className="empty">Operators ingest analytics; all roles can inspect their provenance.</p>}</section>
    <section className="panel"><div className="panel-title"><div><p className="eyebrow">Recoverability</p><h2>Storage, backup and system health</h2></div><span className="status good">scripts verified</span></div><p>PostgreSQL and immutable MinIO objects are backed up together, checksummed, and restored only into disposable drill targets. Production targets are never overwritten by the drill.</p><div className="card-list"><article className="profile-card subject"><div><strong>Backup</strong><small><code>scripts/backup.sh /absolute/new/backup</code></small></div><span className="status good">explicit target</span></article><article className="profile-card subject"><div><strong>Restore drill</strong><small><code>scripts/restore-drill.sh /absolute/backup drill-id</code></small></div><span className="status good">disposable</span></article><article className="profile-card subject"><div><strong>SBOM and security</strong><small>Signed-by-hash CycloneDX inventory and repeatable exposure audit</small></div><span className="status good">operator evidence</span></article></div><p className="muted">Grafana is optional and authenticated. Prometheus, Loki and OpenTelemetry remain on internal networks; only Caddy exposes a host port.</p></section></div>
    <section className="panel"><div className="panel-title"><div><p className="eyebrow">Human review with automatic activation</p><h2>Model recommendations</h2></div><span className="chip">{recommendations.length}</span></div><div className="card-list">{recommendations.map(item => <article className="profile-card subject" key={item.id}><div><strong>{item.task_type.replaceAll("_", " ")} · {(item.score * 100).toFixed(2)}%</strong><small>Primary {models.find(model => model.id === item.recommended_model_id)?.display_name || item.recommended_model_id} · fallbacks {item.recommended_fallback_model_ids.map(id => models.find(model => model.id === id)?.display_name || id.slice(0, 8)).join(", ") || "none"}</small><small>Baseline {item.baseline_assignment_id ? item.baseline_assignment_id.slice(0, 12) : "none"} · route now {activeAssignments[item.task_type]?.id?.slice(0, 12) || "not visible to this role"}</small><small>Hash {item.recommendation_hash.slice(0, 16)}… · {item.reasoning.join(" · ")}</small></div><div className="actions"><span className={`status ${item.applied ? "good" : item.decision === "rejected" ? "bad" : "waiting"}`}>{item.applied ? "applied" : item.decision}</span>{canReview && !item.applied && <button className="secondary compact" onClick={() => decide(item, "rejected")}>Reject</button>}{canReview && !item.applied && <button className="primary compact" onClick={() => decide(item, "approved")}>Approve & apply</button>}</div></article>)}{!recommendations.length && <p className="empty">No benchmark recommendation has been created.</p>}</div></section>
    <details className="panel action-disclosure"><summary><span><small>Operational provenance</small><strong>Acceptance evidence registry</strong><em>Register backup, restore, SBOM, security or observability evidence only when you have a concrete artifact.</em></span><span className="chip">{operationalEvidence.length}</span></summary><div className="card-list">{operationalEvidence.slice(0, 8).map(item => <article className="profile-card subject" key={item.id}><div><strong>{item.evidence_kind.replaceAll("_", " ")}</strong><small>{new Date(item.created_at).toLocaleString()} · {item.content_hash.slice(0, 16)}…</small><small>{item.artifact_hash ? `Artifact ${item.artifact_hash.slice(0, 16)}…` : "Canonical document hash only"}</small></div><span className="status good">recorded</span></article>)}{!operationalEvidence.length && <p className="empty">No operational evidence has been registered yet.</p>}</div>{canAdmin && <form onSubmit={recordEvidence}><div className="form-pair"><label>Evidence kind<select name="evidence_kind"><option value="backup">Backup</option><option value="restore_drill">Restore drill</option><option value="sbom">SBOM</option><option value="security_audit">Security audit</option><option value="observability">Observability</option></select></label><label>Artifact SHA-256 (optional)<input name="artifact_hash" pattern="[0-9a-f]{64}" /></label></div><label>Evidence JSON<textarea name="document" rows={7} defaultValue={'{\n  "result": "passed",\n  "recorded_from": "operator_ui"\n}'} spellCheck={false} required /></label><button className="secondary">Register immutable evidence</button></form>}</details>
    {canAdmin && <details className="panel action-disclosure"><summary><span><small>Recommendation only</small><strong>Controlled model benchmark</strong><em>Create benchmark evidence; routing changes only after the recommendation is approved.</em></span><span className="chip">65 / 20 / 10 / 5</span></summary><form onSubmit={benchmark}><div className="form-pair"><label>Task<select value={taskType} onChange={event => setTaskType(event.target.value)}><option value="script_writer">Script writer</option><option value="script_verifier">Script verifier</option><option value="storyboard">Storyboard</option></select></label><label>Suite version<input name="suite_version" defaultValue="1" required /></label></div><div className="split-grid"><fieldset><legend>Candidate one</legend><label>Model<select value={firstModel} onChange={event => setFirstModel(event.target.value)} required>{models.filter(item => item.enabled && item.visible).map(item => <option key={item.id} value={item.id}>{item.display_name}</option>)}</select></label><div className="form-pair"><label>Quality<input name="quality_one" type="number" min="0" max="100" defaultValue="92" /></label><label>Success<input name="success_one" type="number" min="0" max="1" step="0.01" defaultValue="0.98" /></label></div><div className="form-pair"><label>P95 ms<input name="latency_one" type="number" min="1" defaultValue="800" /></label><label>Mean USD<input name="cost_one" type="number" min="0" step="0.000001" defaultValue="0.02" /></label></div></fieldset><fieldset><legend>Candidate two</legend><label>Model<select value={secondModel} onChange={event => setSecondModel(event.target.value)} required>{models.filter(item => item.enabled && item.visible).map(item => <option key={item.id} value={item.id}>{item.display_name}</option>)}</select></label><div className="form-pair"><label>Quality<input name="quality_two" type="number" min="0" max="100" defaultValue="90" /></label><label>Success<input name="success_two" type="number" min="0" max="1" step="0.01" defaultValue="1" /></label></div><div className="form-pair"><label>P95 ms<input name="latency_two" type="number" min="1" defaultValue="300" /></label><label>Mean USD<input name="cost_two" type="number" min="0" step="0.000001" defaultValue="0" /></label></div></fieldset></div><button className="primary" disabled={!firstModel || !secondModel || firstModel === secondModel}>Store benchmark and recommend</button></form></details>}
    {canOperate && <details className="panel action-disclosure"><summary><span><small>Governance gates</small><strong>Freshness, originality and budget controls</strong><em>Run only when a source age, originality or spend policy needs a manual recorded check.</em></span><span className="chip">{snapshots.length} source snapshots</span></summary><div className="operations-tools split-grid">{canAdmin && <section><div className="panel-title"><div><p className="eyebrow">Pre-publication evidence policy</p><h2>Freshness and originality</h2></div></div><form onSubmit={freshness}><label>Source snapshot<select name="snapshot_id" required>{snapshots.map(item => <option key={item.id} value={item.id}>{item.sourceTitle} · {item.content_hash.slice(0, 10)}…</option>)}</select></label><label>Maximum source age (days)<input name="maximum_age_days" type="number" min="1" max="365" defaultValue="30" /></label><button className="secondary" disabled={!snapshots.length}>Run freshness gate</button></form><form onSubmit={originality}><label>Script version<select name="script_version_id" required>{scripts.map(item => <option key={item.current_version_id} value={item.current_version_id}>{item.title} · {item.content_hash.slice(0, 10)}…</option>)}</select></label><label>Comparison source/key<input name="comparison_key" defaultValue="channel-corpus-v1" required /></label><label>Semantic overlap (0–1)<input name="overlap" type="number" min="0" max="1" step="0.01" defaultValue="0.12" /></label><button className="secondary" disabled={!scripts.length}>Store originality report</button></form></section>}<section><div className="panel-title"><div><p className="eyebrow">Fail-closed spending</p><h2>Budget policy and usage</h2></div></div>{canAdmin && <form onSubmit={budgetPolicy}><label>Scope<input name="scope" defaultValue="channel.fakebuster" pattern="[a-z][a-z0-9_.:-]*" required /></label><div className="form-pair"><label>USD limit<input name="limit_amount" type="number" min="0" step="0.000001" defaultValue="25" /></label><label>Period<select name="period"><option value="workflow">Workflow</option><option value="daily">Daily</option><option value="monthly">Monthly</option></select></label></div><button className="secondary">Activate policy version</button></form>}<form onSubmit={budgetUsage}><label>Scope<input name="scope" defaultValue="channel.fakebuster" required /></label><div className="form-pair"><label>Amount USD<input name="amount" type="number" min="0" step="0.000001" defaultValue="1.25" /></label><label>Category<input name="category" defaultValue="model-inference" pattern="[a-z][a-z0-9_.-]*" required /></label></div><button className="primary">Record idempotent usage</button></form></section></div></details>}
    {canAdmin && <details className="panel action-disclosure"><summary><span><small>Changed or retracted evidence</small><strong>Correction workflow</strong><em>Record a source, script or publication correction and let the impact map determine required release action.</em></span><span className="chip">{snapshots.length + scripts.length + publications.length} targets</span></summary><form onSubmit={correction}><div className="form-pair"><label>Target type<select name="target_type" value={correctionTargetType} onChange={event => setCorrectionTargetType(event.target.value as "source" | "script" | "publication")}><option value="source">Source snapshot</option><option value="script">Script version</option><option value="publication">Publication</option></select></label><label>Target<select name="target_id" required>{correctionTargetType === "source" && snapshots.map(item => <option key={item.id} value={item.id}>Source · {item.sourceTitle} · {item.content_hash.slice(0, 10)}</option>)}{correctionTargetType === "script" && scripts.map(item => <option key={item.current_version_id} value={item.current_version_id}>Script · {item.title}</option>)}{correctionTargetType === "publication" && publications.map(item => <option key={item.id} value={item.id}>Publication · {item.youtube_video_id || item.id.slice(0, 10)}</option>)}</select></label></div><div className="form-pair"><label>Change kind<select name="change_kind"><option value="changed">Changed</option><option value="corrected">Corrected</option><option value="retracted">Retracted</option><option value="unavailable">Unavailable</option></select></label><label>Severity<select name="severity"><option value="low">Low</option><option value="medium">Medium</option><option value="high">High</option><option value="critical">Critical</option></select></label></div><label>Finding<textarea name="finding" minLength={10} rows={4} defaultValue="A source changed or was retracted; identify affected claims and timecodes before taking the required release action." /></label><button className="primary" disabled={correctionTargetType === "source" ? !snapshots.length : correctionTargetType === "script" ? !scripts.length : !publications.length}>Record correction case</button></form></details>}
  </>;
}

function WorkflowPanel({ csrf, activeChannelId, onOpenPage }: { csrf: string; activeChannelId: string; onOpenPage: (page: string) => void }) {
  const [items, setItems] = useState<WorkflowSummary[]>([]);
  const [probe, setProbe] = useState<Probe | null>(null);
  const [message, setMessage] = useState("");
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState("attention");
  const [busy, setBusy] = useState(false);
  const [refreshedAt, setRefreshedAt] = useState<Date | null>(null);
  const refreshActivity = useCallback(async () => {
    setBusy(true);
    try {
      setItems(await api<WorkflowSummary[]>("/api/v1/system/workflows?limit=100"));
      setRefreshedAt(new Date()); setMessage("");
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : "Workflow activity could not be loaded"); }
    finally { setBusy(false); }
  }, []);
  useEffect(() => { refreshActivity(); }, [refreshActivity]);
  async function startProbe() { try { const idempotency_key = randomUuid(); setProbe(await api<Probe>("/api/v1/system/durability-probes", { method: "POST", body: JSON.stringify({ idempotency_key }) }, csrf)); setMessage("Durability test is waiting. Recreate the worker, then complete the test."); await refreshActivity(); } catch (e) { setMessage(e instanceof Error ? e.message : "Durability test failed to start"); } }
  async function refreshProbe() { if (probe) setProbe(await api<Probe>(`/api/v1/system/durability-probes/${probe.workflow_id}`)); }
  async function completeProbe() { if (probe) { setProbe(await api<Probe>(`/api/v1/system/durability-probes/${probe.workflow_id}/complete`, { method: "POST" }, csrf)); await refreshActivity(); } }
  const scoped = activeChannelId ? items.filter(item => item.channel_profile_id === activeChannelId) : items;
  const needsAttention = (status: WorkflowSummary["execution_status"]) => ["FAILED", "CANCELLED", "TERMINATED", "TIMED_OUT", "UNKNOWN"].includes(status);
  const normalizedQuery = query.trim().toLowerCase();
  const visible = scoped.filter(item => {
    if (statusFilter === "running" && item.execution_status !== "RUNNING") return false;
    if (statusFilter === "attention" && !needsAttention(item.execution_status)) return false;
    if (statusFilter === "completed" && item.execution_status !== "COMPLETED") return false;
    return !normalizedQuery || [item.workflow_type, item.workflow_id, item.channel_name, item.subject_name].some(value => value?.toLowerCase().includes(normalizedQuery));
  });
  const stageFor = (type: string) => type.startsWith("youtube-") ? "publishing" : type.includes("media") || type.includes("narration") ? "media" : type.includes("script") || type.includes("storyboard") || type.includes("scene-alternative") ? "editorial" : type === "durability-probe" ? "workflows" : "research";
  const stageAdvice = (stage: string) => stage === "publishing" ? "Check upload, metadata and release prerequisites." : stage === "media" ? "Check narration, render inputs and media QA." : stage === "editorial" ? "Check script/storyboard versions and approval state." : stage === "workflows" ? "Inspect the durability test controls below." : "Check source acquisition, research run logs and dossier state.";
  const attentionItems = scoped.filter(item => needsAttention(item.execution_status));
  const attentionGroups = Array.from(attentionItems.reduce((groups, item) => {
    const current = groups.get(item.workflow_type) || { type: item.workflow_type, count: 0, latest: item };
    current.count += 1;
    if (new Date(item.created_at).getTime() > new Date(current.latest.created_at).getTime()) current.latest = item;
    groups.set(item.workflow_type, current);
    return groups;
  }, new Map<string, { type: string; count: number; latest: WorkflowSummary }>()).values()).sort((left, right) => right.count - left.count || new Date(right.latest.created_at).getTime() - new Date(left.latest.created_at).getTime()).slice(0, 4);
  return <><div className="page-heading"><div><p className="eyebrow">Cross-channel orchestration</p><h1>Workflow activity</h1><p className="muted">Find running work, failures and completed jobs without memorizing workflow IDs.</p></div><button className="secondary" disabled={busy} onClick={refreshActivity}>{busy ? "Refreshing…" : "Refresh activity"}</button></div>
    {message && <div className="notice">{message}</div>}
    <div className="workflow-summary"><article><span>Running</span><strong>{scoped.filter(item => item.execution_status === "RUNNING").length}</strong></article><article><span>Needs attention</span><strong>{scoped.filter(item => needsAttention(item.execution_status)).length}</strong></article><article><span>Completed</span><strong>{scoped.filter(item => item.execution_status === "COMPLETED").length}</strong></article></div>
    {attentionGroups.length > 0 && <section className="panel workflow-triage"><div className="panel-title"><div><p className="eyebrow">Failure triage</p><h2>Start with the repeated failure groups</h2><p className="muted">Grouped by workflow type so one underlying issue does not look like dozens of unrelated tasks.</p></div></div><div className="workflow-triage-grid">{attentionGroups.map(group => {
      const stage = stageFor(group.type);
      return <article key={group.type}><div><span className="status bad">{group.count} need attention</span><strong>{group.type.replaceAll("-", " ")}</strong><small>Latest: {group.latest.subject_name || group.latest.channel_name || "System workflow"} · {new Date(group.latest.created_at).toLocaleString()}</small><p>{stageAdvice(stage)}</p></div><div className="actions"><button type="button" className="secondary compact" onClick={() => { setStatusFilter("attention"); setQuery(group.type); }}>Show group</button><button type="button" className="primary compact" onClick={() => onOpenPage(stage)}>{stage === "workflows" ? "View here" : "Open stage"}</button></div></article>;
    })}</div></section>}
    <section className="panel"><div className="workflow-toolbar"><label>Find workflow<input type="search" value={query} onChange={event => setQuery(event.target.value)} placeholder="Type, subject, channel or ID" /></label><label>Status<select value={statusFilter} onChange={event => setStatusFilter(event.target.value)}><option value="attention">Needs attention</option><option value="running">Running</option><option value="completed">Completed</option><option value="all">All activity</option></select></label><small className="muted">{refreshedAt ? `Updated ${refreshedAt.toLocaleTimeString()}` : "Loading activity…"}</small></div><div className="workflow-activity-list">{visible.map(item => <article key={item.workflow_id}><div className="workflow-kind"><span className={`status ${item.execution_status === "COMPLETED" ? "good" : needsAttention(item.execution_status) ? "bad" : "waiting"}`}>{item.execution_status.replaceAll("_", " ").toLowerCase()}</span><strong>{item.workflow_type.replaceAll("-", " ")}</strong></div><div><strong>{item.subject_name || item.channel_name || "System workflow"}</strong><small>{item.channel_name || "No channel assigned"} · {new Date(item.created_at).toLocaleString()}</small><code title={item.workflow_id}>{item.workflow_id}</code>{item.parent_workflow_id && <small>Retry of {item.parent_workflow_id}</small>}</div><button className="secondary compact" onClick={() => onOpenPage(stageFor(item.workflow_type))}>{stageFor(item.workflow_type) === "workflows" ? "View here" : "Open stage"}</button></article>)}{!visible.length && <p className="empty">No workflows match this channel and status. Choose “All activity” to inspect history.</p>}</div></section>
    <details className="panel registry-panel"><summary>Advanced: test restart durability</summary><div className="probe"><p className="muted">This operator-only test starts a waiting Temporal workflow so restart recovery can be checked without touching editorial work.</p>{probe && <div className="probe-state"><span className={`status ${probe.state === "COMPLETED" ? "good" : "waiting"}`}>{probe.state}</span><code>{probe.workflow_id}</code><small>Started {probe.started_at ? new Date(probe.started_at).toLocaleString() : "—"}</small></div>}<div className="actions"><button className="secondary" onClick={startProbe}>Start durability test</button><button className="secondary" disabled={!probe} onClick={refreshProbe}>Refresh test</button><button className="secondary" disabled={!probe || probe.state === "COMPLETED"} onClick={completeProbe}>Complete test</button></div></div></details>
  </>;
}

export default function Home() {
  const [loading, setLoading] = useState(true); const [bootstrap, setBootstrap] = useState(false); const [session, setSession] = useState<Session | null>(null); const [capabilities, setCapabilities] = useState<Capabilities | null>(null); const [page, setPage] = useState("dashboard");
  const [channels, setChannels] = useState<ChannelProfile[]>([]); const [activeChannelId, setActiveChannelId] = useState(""); const [helpOpen, setHelpOpen] = useState(false);
  useContextualUsageHints(`${page}:${loading}:${session?.user.id || "anonymous"}`);
  useEffect(() => { Promise.all([api<{ required: boolean }>("/api/v1/auth/bootstrap-status"), api<Session>("/api/v1/auth/session").catch(() => null)]).then(([status, current]) => { setBootstrap(status.required); setSession(current); }).finally(() => setLoading(false)); }, []);
  useEffect(() => { if (session) Promise.all([api<Capabilities>("/api/v1/system/capabilities"), api<ChannelProfile[]>("/api/v1/channel-profiles")]).then(([nextCapabilities, nextChannels]) => { setCapabilities(nextCapabilities); setChannels(nextChannels); const stored = window.localStorage.getItem("tubefactory.channel") || window.localStorage.getItem("evidence-studio.channel") || ""; if (stored) window.localStorage.setItem("tubefactory.channel", stored); setActiveChannelId(nextChannels.some(item => item.id === stored) ? stored : ""); }); }, [session]);
  useEffect(() => { const syncPage = () => { const requested = new URL(window.location.href).searchParams.get("page") || "dashboard"; setPage(PAGE_IDS.has(requested) ? requested : "dashboard"); }; syncPage(); window.addEventListener("popstate", syncPage); return () => window.removeEventListener("popstate", syncPage); }, []);
  if (loading) return <main className="loading"><div className="brand-mark">TF</div><span>Loading control plane…</span></main>;
  if (!session) return <AuthScreen bootstrap={bootstrap} onSession={value => { setSession(value); setBootstrap(false); }} />;
  const navGroups = [
    { label: "Workspace", items: [
      { id: "dashboard", label: "Overview", help: "Check system health, workflow counts and available capabilities for this workspace." },
      { id: "workflows", label: "Workflow activity", help: "Find running, failed and completed workflows, then open the stage that needs attention.", roles: ["admin", "operator"] },
    ] },
    { label: "Create and review", items: [
      { id: "profiles", label: "Channels & subjects", help: "Create channels, define subject profiles and control scheduled discovery." },
      { id: "research", label: "Research & evidence", help: "Review opportunities, collect sources, verify claims and approve dossiers." },
      { id: "editorial", label: "Scripts & storyboards", help: "Generate evidence-linked scripts, review versions and plan scenes." },
      { id: "media", label: "Media & final review", help: "Produce narration and renders, inspect quality checks and approve final media." },
      { id: "publishing", label: "Publishing & calendar", help: "Create exact metadata, upload privately and separately approve release." },
    ] },
    { label: "Operations", items: [
      { id: "operations", label: "Analytics & operations", help: "Ingest performance metrics and manage operational evidence, budgets and corrections." },
      { id: "audit", label: "Audit log", help: "Trace immutable operator and system actions." },
    ] },
    { label: "Administration", items: [
      { id: "archived", label: "Archive", help: "Search archived Channels, subjects and scored findings outside the active worklists." },
      { id: "providers", label: "Providers & prompts", help: "Configure model providers, prompts and task assignments.", roles: ["admin"] },
      { id: "configuration", label: "Configuration", help: "Review and activate versioned application settings.", roles: ["admin"] },
      { id: "users", label: "Users", help: "Create accounts, assign roles and manage access.", roles: ["admin"] },
      { id: "identity", label: "Authentication", help: "Manage your password, multi-factor authentication, recovery codes and OIDC." },
    ] },
  ];
  const activeChannel = channels.find(item => item.id === activeChannelId) || null;
  function chooseChannel(value: string) { setActiveChannelId(value); window.localStorage.setItem("tubefactory.channel", value); }
  function choosePage(value: string) { if (!PAGE_IDS.has(value)) return; setPage(value); const url = new URL(window.location.href); if (value === "dashboard") url.searchParams.delete("page"); else url.searchParams.set("page", value); window.history.pushState(null, "", url); }
  async function refreshChannelChoices() {
    const nextChannels = await api<ChannelProfile[]>("/api/v1/channel-profiles");
    setChannels(nextChannels);
    setActiveChannelId(current => {
      if (!current || nextChannels.some(channel => channel.id === current)) return current;
      window.localStorage.setItem("tubefactory.channel", "");
      return "";
    });
  }
  return <><div className="app-shell"><aside><div className="brand"><div className="brand-mark">TF</div><div><strong>TubeFactory</strong><small>Editorial control plane</small></div><UsageTip text="Search longer procedures by task, or hover and focus controls for concise guidance." onClick={() => setHelpOpen(true)} /></div><label className="channel-switcher">Channel workspace<select aria-label="Channel workspace" value={activeChannelId} onChange={event => chooseChannel(event.target.value)}><option value="">All channels</option>{channels.map(channel => <option key={channel.id} value={channel.id}>{channel.name}</option>)}</select><small>{activeChannel ? `${activeChannel.enabled ? "Enabled" : "Disabled"} · ${activeChannel.languages.join(", ")}` : `${channels.length} channels in portfolio`}</small></label><nav aria-label="Main navigation">{navGroups.map(group => { const visible = group.items.filter(item => !item.roles || item.roles.includes(session.user.role)); return visible.length ? <div className="nav-group" key={group.label}><span>{group.label}</span>{visible.map(item => <button key={item.id} className={page === item.id ? "active nav-help" : "nav-help"} data-usage={item.help} title={item.help} aria-label={item.label + ". " + item.help} onClick={() => choosePage(item.id)}>{item.label}</button>)}</div> : null; })}</nav><div className="profile"><span className="avatar">{session.user.display_name.slice(0, 2).toUpperCase()}</span><span><strong>{session.user.display_name}</strong><small>{session.user.role}</small></span><button className="sign-out" onClick={async () => { await api("/api/v1/auth/logout", { method: "POST" }, session.csrf_token); setSession(null); }}>Sign out</button></div></aside><main className="workspace"><div className="scope-bar"><div><span className="eyebrow">Current workspace</span><strong>{activeChannel?.name || "All channels"}</strong></div><span>{activeChannel ? "Every channel-aware queue is filtered to this channel." : "Portfolio view across every channel."}</span></div><ProductionPath currentPage={page} onOpenPage={choosePage} />{page === "dashboard" && <Dashboard capabilities={capabilities} activeChannelId={activeChannelId} />}{page === "profiles" && <ProfilesPanel csrf={session.csrf_token} role={session.user.role} activeChannelId={activeChannelId} onChannelsChanged={refreshChannelChoices} />}{page === "archived" && <ArchivedProfilesPanel csrf={session.csrf_token} role={session.user.role} onChannelsChanged={refreshChannelChoices} />}{page === "research" && <ResearchPanel csrf={session.csrf_token} role={session.user.role} activeChannelId={activeChannelId} />}{page === "editorial" && <EditorialPanel csrf={session.csrf_token} role={session.user.role} activeChannelId={activeChannelId} />}{page === "media" && <MediaPanel csrf={session.csrf_token} role={session.user.role} activeChannelId={activeChannelId} onOpenPage={choosePage} />}{page === "publishing" && <PublishingPanel csrf={session.csrf_token} role={session.user.role} activeChannelId={activeChannelId} onOpenPage={choosePage} />}{page === "operations" && <OperationsPanel csrf={session.csrf_token} role={session.user.role} activeChannelId={activeChannelId} />}{page === "identity" && <IdentityPanel session={session} onSession={setSession} />}{page === "providers" && <ProviderPanel csrf={session.csrf_token} />}{page === "workflows" && <WorkflowPanel csrf={session.csrf_token} activeChannelId={activeChannelId} onOpenPage={choosePage} />}{page === "configuration" && <ConfigPanel csrf={session.csrf_token} />}{page === "users" && <UsersPanel csrf={session.csrf_token} />}{page === "audit" && <AuditPanel />}</main></div><HelpPanel open={helpOpen} onClose={() => setHelpOpen(false)} currentPage={page} role={session.user.role} onOpenPage={choosePage} /></>;
}
