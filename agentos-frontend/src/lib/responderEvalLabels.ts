/** Human-readable labels for responder eval rubric metrics (non-ambiguous). */

export type MetricDisplay = {
  value: string;
  meaning: string;
};

/** 0–100 score → one-word band. */
export function scoreBandLabel(score: number | null | undefined): string {
  if (score == null || Number.isNaN(score)) return "Unavailable";
  if (score >= 90) return "Excellent";
  if (score >= 75) return "Good";
  if (score >= 50) return "Fair";
  if (score >= 25) return "Poor";
  return "Critical";
}

export function scoreMetric(score: number | null | undefined): MetricDisplay {
  if (score == null || Number.isNaN(score)) {
    return { value: "—", meaning: "Not scored" };
  }
  return { value: String(score), meaning: scoreBandLabel(score) };
}

const RESOLUTION_LABELS: Record<string, MetricDisplay> = {
  yes: { value: "Yes", meaning: "Issue resolved" },
  partial: { value: "Partial", meaning: "Partly resolved" },
  no: { value: "No", meaning: "Unresolved" },
  not_applicable: { value: "N/A", meaning: "No agent reply" },
};

export function resolutionMetric(resolution: unknown): MetricDisplay {
  const key = String(resolution || "unknown").toLowerCase();
  return RESOLUTION_LABELS[key] ?? { value: String(resolution ?? "—"), meaning: "Unknown" };
}

export function hallucinationMetric(
  flagged: unknown,
  factsMissed?: unknown,
  turns?: unknown
): MetricDisplay {
  if (flagged === true) {
    const n = Math.max(
      1,
      Number(factsMissed) || (Array.isArray(turns) ? turns.length : 0) || 1
    );
    return {
      value: "Yes",
      meaning: n === 1 ? "1 fact missed" : `${n} facts missed`,
    };
  }
  return { value: "No", meaning: "" };
}

export function notGradedReasonLabel(reason: unknown): string {
  const key = String(reason || "").toLowerCase();
  const map: Record<string, string> = {
    no_agent_turns: "No agent replies in chat",
    not_applicable: "Judge marked not applicable",
  };
  return map[key] ?? (key ? key.replaceAll("_", " ") : "Unknown");
}

export function guardrailScoreFromViolations(violations: unknown): number {
  const n = Math.max(0, Number(violations) || 0);
  return Math.max(0, 100 - 25 * n);
}

export function guardrailMetric(violations: unknown): MetricDisplay {
  const n = Math.max(0, Number(violations) || 0);
  const score = guardrailScoreFromViolations(n);
  return {
    value: String(score),
    meaning: n === 0 ? "No violations" : n === 1 ? "1 violation" : `${n} violations`,
  };
}

export function traceabilityMetric(
  score: unknown,
  opts?: { pass?: unknown; skipped?: unknown }
): MetricDisplay {
  if (opts?.skipped === true) {
    return { value: "—", meaning: "Skipped (no ground truth)" };
  }
  if (score != null && score !== "" && !Number.isNaN(Number(score))) {
    const n = Number(score);
    return { value: String(n), meaning: scoreBandLabel(n) };
  }
  if (opts?.pass === true) {
    return { value: "Pass", meaning: "Grounded" };
  }
  if (opts?.pass === false) {
    return { value: "Fail", meaning: "Ungrounded" };
  }
  return { value: "—", meaning: "Not available" };
}

export function evalStatusLabel(status: string): string {
  if (status === "not_graded") return "Not graded";
  if (status === "completed") return "Graded";
  return status.replaceAll("_", " ");
}

export function letterGradeMeaning(grade: string): string {
  const g = (grade || "").trim().toUpperCase();
  const map: Record<string, string> = {
    A: "Excellent",
    B: "Good",
    C: "Fair",
    D: "Below standard",
    E: "Failed",
    "-": "Not graded",
  };
  return map[g] ?? "Unknown";
}

export function compositeMetric(score: number | null | undefined, letterGrade: string): MetricDisplay {
  if (score == null) {
    return { value: "—", meaning: letterGradeMeaning(letterGrade) };
  }
  return { value: String(score), meaning: letterGradeMeaning(letterGrade) };
}

const STRUCTURAL_FLAG_LABELS: Record<string, string> = {
  low_turn_count: "Few turns in transcript",
  imbalanced_talk_ratio: "AI Bot spoke much more than customer",
  long_bot_monologue: "Very long single AI Bot message",
};

export function structuralFlagLabel(flag: string): string {
  return STRUCTURAL_FLAG_LABELS[flag] ?? flag.replaceAll("_", " ");
}

export function formatTurnList(turns: unknown): string {
  if (!Array.isArray(turns) || turns.length === 0) return "None";
  return turns.map((t) => String(t)).join(", ");
}

/** Display label for chat transcript roles (distinct from PII placeholders [customer]/[agent]). */
export function chatRoleLabel(role: unknown): string {
  const r = String(role || "").toLowerCase();
  if (r === "user") return "Customer";
  if (r === "agent") return "Human agent";
  if (r === "bot" || r === "assistant") return "AI Bot";
  return String(role || "Unknown");
}

export type SegmentEval = {
  segment?: string;
  turn_indexes?: number[];
  graded?: boolean;
  composite_score?: number | null;
  letter_grade?: string;
  resolution?: string;
  policy_score?: number;
  tonality_score?: number;
  sentiment_score?: number;
  guardrail_violations?: number;
  hallucination_flagged?: boolean;
  hallucination_turns?: unknown;
  hallucination_facts_missed?: number;
  traceability_score?: number;
  traceability_pass?: boolean;
  traceability_skipped?: boolean;
  untraceable_turns?: unknown;
};

export type RubricMetricItem = {
  label: string;
  metric: MetricDisplay;
  hint?: string;
};

/** Build rubric metric cards for overall eval or a bot/human segment. */
export function buildRubricMetricItems(
  source: Record<string, unknown>,
  graded: boolean,
  opts?: { traceMode?: unknown; isSegment?: boolean }
): RubricMetricItem[] {
  const traceMode = opts?.traceMode;
  const traceScore = source.traceability_score;
  if (!graded) {
    return [
      { label: "Resolution", metric: resolutionMetric(source.resolution) },
      {
        label: "Traceability",
        metric: traceabilityMetric(traceScore, {
          pass: source.traceability_pass,
          skipped: source.traceability_skipped,
        }),
      },
    ];
  }
  return [
    { label: "Policy adherence", metric: scoreMetric(source.policy_score as number | null) },
    { label: "Resolution", metric: resolutionMetric(source.resolution) },
    { label: "Tonality", metric: scoreMetric(source.tonality_score as number | null) },
    { label: "Sentiment", metric: scoreMetric(source.sentiment_score as number | null) },
    { label: "Guardrails", metric: guardrailMetric(source.guardrail_violations) },
    {
      label: "Hallucination",
      metric: hallucinationMetric(
        source.hallucination_flagged,
        source.hallucination_facts_missed,
        source.hallucination_turns
      ),
    },
    {
      label: "Traceability",
      metric: traceabilityMetric(traceScore, {
        pass: source.traceability_pass,
        skipped: source.traceability_skipped,
      }),
      hint:
        !opts?.isSegment && traceMode
          ? `Source: ${String(traceMode).replaceAll("_", " ")}`
          : opts?.isSegment
            ? "Segment turns only"
            : undefined,
    },
  ];
}

export function segmentEvalTitle(key: string): string {
  if (key === "bot") return "AI Bot";
  if (key === "human_agent") return "Human agent";
  return key.replaceAll("_", " ");
}

/** Plain-text view of chat HTML from the source system. */
export function chatMessagePlainText(content: unknown): string {
  if (content == null) return "";
  let text = String(content);
  text = text.replace(/<br\s*\/?>/gi, "\n");
  text = text.replace(/<\/p>/gi, "\n");
  text = text.replace(/<a[^>]*href=["']([^"']+)["'][^>]*>(.*?)<\/a>/gi, "$2 ($1)");
  text = text.replace(/<[^>]+>/g, "");
  return text.replace(/\n{3,}/g, "\n\n").trim();
}

const LIFECYCLE_LABELS: Record<string, string> = {
  pre_delivery: "Pre-delivery",
  in_transit: "In transit",
  delivered: "Delivered",
  refund: "Refund",
  cancellation: "Cancellation",
  unknown: "Unknown",
  cannot_assess: "Cannot assess",
};

export function lifecycleStageLabel(stage: unknown): string {
  const key = String(stage || "").toLowerCase();
  return LIFECYCLE_LABELS[key] ?? (key.replaceAll("_", " ") || "—");
}

const GUARDRAIL_RULE_LABELS: Record<string, string> = {
  no_solution_before_concern: "No solution before concern",
  no_subjective_self_description: "No subjective self-description",
  explain_absence_dont_repeat: "Explain absence — don't repeat",
  order_selection_before_action: "Order selection before action",
};

export function guardrailRuleLabel(rule: string): string {
  return GUARDRAIL_RULE_LABELS[rule] ?? rule.replaceAll("_", " ");
}

export function hardGateReasonLabel(reason: unknown): string {
  const key = String(reason || "");
  const map: Record<string, string> = {
    pii_incident: "PII leak detected after redaction",
    // Chart parent `hard_gate:pii` — keep concise; detail surface uses pii_incident.
    pii: "PII leak (after redaction)",
    inherited_pii: "Grade E forced by chat PII (not attributed to this speaker)",
    inherited_hallucination:
      "Grade E forced by chat hallucination (not this speaker's turns)",
    hallucination: "Hallucination hard gate",
    hallucination_P0: "Critical hallucination (P0)",
    hallucination_P1: "Major hallucination (P1)",
  };
  return map[key] ?? key.replaceAll("_", " ");
}

const HALLUCINATION_MODE_LABELS: Record<string, string> = {
  false_refund: "False refund claim",
  false_shipment: "False shipment claim",
  false_delivery: "False delivery claim",
  false_cancellation: "False cancellation claim",
  false_refund_processed: "False refund-processed claim",
  llm_flagged_no_claim_keyword: "Flagged without claim keyword",
};

const POLICY_BUCKET_LABELS: Record<string, string> = {
  order_selection: "Order selection",
  explain_absence: "Explain absence",
  unsupported_status_or_eta: "Unsupported status/ETA",
  unsupported_refund: "Unsupported refund claim",
  unsupported_delivery: "Unsupported delivery claim",
  unsupported_cancellation: "Unsupported cancellation claim",
  tone: "Tone / professionalism",
  other: "Other policy issue",
};

const TRACEABILITY_REASON_LABELS: Record<string, string> = {
  unsupported_refund: "Unsupported refund claim",
  unsupported_shipment: "Unsupported shipment claim",
  unsupported_delivery: "Unsupported delivery claim",
  unsupported_cancellation: "Unsupported cancellation claim",
  nuance_or_paraphrase: "Nuance / paraphrase mismatch",
  no_claim_keyword: "Untraceable without claim keyword",
  score_disagreement: "Det vs LLM score disagreement",
  other: "Other traceability issue",
};

export function evalIssueAttributeLabel(parentIssue: string, attrId: string): string {
  if (attrId === "severity_soft") return "Soft (no hard gate)";
  if (attrId.startsWith("severity_")) {
    return `Severity ${attrId.slice("severity_".length)}`;
  }
  if (parentIssue === "hard_gate:pii" || parentIssue.startsWith("pii")) {
    if (attrId === "email") return "Email";
    if (attrId === "phone") return "Phone";
    if (attrId === "address") return "Address";
    if (attrId.startsWith("field:")) {
      const field = attrId.slice("field:".length).replaceAll("_", " ");
      return `Field: ${field}`;
    }
    return attrId.replaceAll("_", " ");
  }
  if (
    parentIssue === "hard_gate:hallucination" ||
    parentIssue === "hallucination" ||
    parentIssue.startsWith("hallucination:")
  ) {
    return HALLUCINATION_MODE_LABELS[attrId] ?? attrId.replaceAll("_", " ");
  }
  if (parentIssue === "traceability_failure") {
    return TRACEABILITY_REASON_LABELS[attrId] ?? attrId.replaceAll("_", " ");
  }
  if (parentIssue.startsWith("resolution:")) {
    const stage = lifecycleStageLabel(attrId);
    return stage === "—" ? "Lifecycle context" : `Lifecycle · ${stage}`;
  }
  return attrId.replaceAll("_", " ");
}

export function evalIssueLabel(issue: string): string {
  const [kind, ...rest] = issue.split(":");
  const detail = rest.join(":");
  if (kind === "guardrail") return guardrailRuleLabel(detail);
  if (kind === "policy") {
    return `Policy: ${POLICY_BUCKET_LABELS[detail] ?? detail.replaceAll("_", " ")}`;
  }
  if (kind === "hard_gate") return hardGateReasonLabel(detail);
  if (kind === "hallucination_severity") return `Hallucination severity ${detail}`;
  if (kind === "pii") return `PII: ${detail.replaceAll("_", " ")}`;
  if (kind === "hallucination" && detail) {
    return HALLUCINATION_MODE_LABELS[detail] ?? detail.replaceAll("_", " ");
  }
  if (kind === "traceability" && detail) {
    return TRACEABILITY_REASON_LABELS[detail] ?? detail.replaceAll("_", " ");
  }
  if (kind === "resolution_lifecycle") return `Lifecycle · ${lifecycleStageLabel(detail)}`;
  if (issue === "hallucination") return "Hallucination";
  if (issue === "traceability_failure") return "Traceability failure";
  if (issue === "guardrail_violations") {
    return "Guardrail count (rule turns not attributed to this speaker)";
  }
  if (issue === "resolution:no") return "Resolution: no";
  if (issue === "resolution:partial") return "Resolution: partial";
  return issue.replaceAll("_", " ");
}
