import { apiJson } from "./api";
import {
  evalIssueAttributeLabel,
  evalIssueLabel,
} from "./responderEvalLabels";

export type ResponderEvalSegment =
  | "composite"
  | "bot"
  | "bot_closed"
  | "bot_pre_handoff"
  | "human";

/** Chart/table segment toggle — AI Bot split into closed vs handed-off chats. */
export const RESPONDER_EVAL_SEGMENTS: { id: ResponderEvalSegment; label: string }[] = [
  { id: "composite", label: "Composite" },
  { id: "bot_closed", label: "AI Bot · closed" },
  { id: "bot_pre_handoff", label: "AI Bot · with hand-off" },
  { id: "human", label: "Human" },
];

export const RESPONDER_EVAL_ISSUE_GRADES = ["E", "D", "C"] as const;
export type ResponderEvalIssueGrade = (typeof RESPONDER_EVAL_ISSUE_GRADES)[number];

export type ResponderEvalSummary = {
  id: string;
  chat_id: string;
  order_id: string | null;
  composite_score: number | null;
  letter_grade: string;
  bot_grade: string;
  human_grade: string;
  eval_status: string;
  eval_version: string;
  created_at: string;
};

export type ResponderEvalList = {
  items: ResponderEvalSummary[];
  total: number;
};

export type ResponderEvalDetail = {
  id: string;
  chat_id: string;
  order_id: string | null;
  rca_run_id: string | null;
  composite_score: number | null;
  letter_grade: string;
  eval_version: string;
  eval_status: string;
  eval: Record<string, unknown>;
  chat?: Record<string, unknown>;
  eval_ground_truth?: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

export type ResponderEvalSummaryStats = {
  window_days: number;
  evaluated: number;
  graded: number;
  not_graded: number;
  graded_bot_runs: number;
  graded_bot_closed_runs?: number;
  graded_bot_pre_handoff_runs?: number;
  graded_human_runs: number;
  pending_dumps: number;
  waiting_chats: number;
  processing_dumps?: number;
  avg_composite_score: number | null;
  avg_bot_score: number | null;
  avg_bot_closed_score?: number | null;
  avg_bot_pre_handoff_score?: number | null;
  avg_human_score: number | null;
  queue_items?: ResponderEvalPendingDump[];
  queue_total?: number;
};

type GradeSlices = { slices: { grade: string; count: number }[]; na_count: number };
type BucketBlock = { buckets: { range: string; count: number }[] };
type ResolutionBlock = { slices: { resolution: string; count: number }[] };
type IssueAttribute = { id: string; count: number };
type IssueRow = { issue: string; count: number; attributes?: IssueAttribute[] };
type IssuesByGrade = Record<ResponderEvalIssueGrade, IssueRow[]>;

export type ResponderEvalGradeDistribution = {
  window_days: number;
  composite: GradeSlices;
  bot: GradeSlices;
  bot_closed: GradeSlices;
  bot_pre_handoff: GradeSlices;
  human: GradeSlices;
};

export type ResponderEvalTimeseries = {
  window_days: number;
  granularity: string;
  points: {
    bucket_start: string;
    total: number;
    total_bot: number;
    total_bot_closed: number;
    total_bot_pre_handoff: number;
    total_human: number;
    avg_score: number | null;
    avg_bot_score: number | null;
    avg_bot_closed_score: number | null;
    avg_bot_pre_handoff_score: number | null;
    avg_human_score: number | null;
  }[];
};

export type ResponderEvalScoreBuckets = {
  window_days: number;
  composite: BucketBlock;
  bot: BucketBlock;
  bot_closed: BucketBlock;
  bot_pre_handoff: BucketBlock;
  human: BucketBlock;
};

export type ResponderEvalResolutionDistribution = {
  window_days: number;
  composite: ResolutionBlock;
  bot: ResolutionBlock;
  bot_closed: ResolutionBlock;
  bot_pre_handoff: ResolutionBlock;
  human: ResolutionBlock;
};

export type ResponderEvalTopIssues = {
  window_days: number;
  composite: IssuesByGrade;
  bot: IssuesByGrade;
  bot_closed: IssuesByGrade;
  bot_pre_handoff: IssuesByGrade;
  human: IssuesByGrade;
};

export type ResponderEvalPendingDump = {
  chat_id: string;
  order_id: string;
  run_id: string;
  eval_status: string;
  retry_count: number;
  last_error: string | null;
  created_at: string;
  updated_at: string;
};

export type ResponderEvalPendingList = {
  items: ResponderEvalPendingDump[];
  total: number;
};

export async function listResponderEvals(params?: {
  limit?: number;
  offset?: number;
  days?: number;
  grade?: string;
  segment?: ResponderEvalSegment;
  eval_status?: string;
  search?: string;
  reason?: string;
}): Promise<ResponderEvalList> {
  const q = new URLSearchParams();
  if (params?.limit != null) q.set("limit", String(params.limit));
  if (params?.offset != null) q.set("offset", String(params.offset));
  if (params?.days != null) q.set("days", String(params.days));
  if (params?.grade) q.set("grade", params.grade);
  if (params?.segment) q.set("segment", params.segment);
  if (params?.eval_status) q.set("eval_status", params.eval_status);
  if (params?.search) q.set("search", params.search);
  if (params?.reason) q.set("reason", params.reason);
  const suffix = q.toString() ? `?${q}` : "";
  return apiJson<ResponderEvalList>(`/api/responder-evals${suffix}`);
}

export async function getResponderEval(id: string): Promise<ResponderEvalDetail> {
  return apiJson<ResponderEvalDetail>(`/api/responder-evals/runs/${id}`);
}

export type ResponderEvalChartsBundle = {
  summary: ResponderEvalSummaryStats;
  grades: ResponderEvalGradeDistribution;
  timeseries: ResponderEvalTimeseries;
  buckets: ResponderEvalScoreBuckets;
  resolutions: ResponderEvalResolutionDistribution;
  top_issues: ResponderEvalTopIssues;
};

export async function getResponderEvalCharts(
  days = 7,
  granularity: "day" | "week" = "day"
): Promise<ResponderEvalChartsBundle> {
  return apiJson<ResponderEvalChartsBundle>(
    `/api/responder-evals/charts?days=${days}&granularity=${granularity}&issues_limit=10`
  );
}

export async function getResponderEvalSummary(days = 7): Promise<ResponderEvalSummaryStats> {
  return apiJson<ResponderEvalSummaryStats>(`/api/responder-evals/summary?days=${days}`);
}

export async function getResponderEvalGradeDistribution(
  days = 7
): Promise<ResponderEvalGradeDistribution> {
  return apiJson<ResponderEvalGradeDistribution>(`/api/responder-evals/grade-distribution?days=${days}`);
}

export async function getResponderEvalTimeseries(
  days = 7,
  granularity: "day" | "week" = "day"
): Promise<ResponderEvalTimeseries> {
  return apiJson<ResponderEvalTimeseries>(
    `/api/responder-evals/timeseries?days=${days}&granularity=${granularity}`
  );
}

export async function getResponderEvalScoreBuckets(days = 7): Promise<ResponderEvalScoreBuckets> {
  return apiJson<ResponderEvalScoreBuckets>(`/api/responder-evals/score-buckets?days=${days}`);
}

export async function getResponderEvalPendingDumps(
  limit = 25,
  offset = 0
): Promise<ResponderEvalPendingList> {
  return apiJson<ResponderEvalPendingList>(
    `/api/responder-evals/pending-dumps?limit=${limit}&offset=${offset}`
  );
}

export async function retryResponderEvalDump(chatId: string): Promise<void> {
  await apiJson<void>(`/api/responder-evals/dumps/${encodeURIComponent(chatId)}/retry`, {
    method: "POST",
  });
}

export async function getResponderEvalTopIssues(
  days = 7,
  limit = 10
): Promise<ResponderEvalTopIssues> {
  return apiJson<ResponderEvalTopIssues>(
    `/api/responder-evals/top-issues?days=${days}&limit=${limit}`
  );
}

export async function getResponderEvalResolutionDistribution(
  days = 7
): Promise<ResponderEvalResolutionDistribution> {
  return apiJson<ResponderEvalResolutionDistribution>(
    `/api/responder-evals/resolution-distribution?days=${days}`
  );
}

export type HandoffMover = {
  id: string;
  label: string;
  bucket: string;
  bucket_label: string;
  count: number;
  delta: number;
  delta_pct: number | null;
};

export type HandoffMatrixRow = {
  id: string;
  label: string;
  days: number[];
  pct_days: number[];
  last_7: number;
  pct_last_7: number;
  mtd: number;
  pct_mtd: number;
  delta: number;
};

export type HandoffMatrixBucket = HandoffMatrixRow & {
  sub_count: number;
  sub_buckets: HandoffMatrixRow[];
};

export type HandoffDayHeader = {
  date: string;
  header: string;
  handoffs: number;
  chats_landed: number;
};

export type HandoffKpis = {
  handoffs_d1: number;
  handoffs_vs_7d_avg_pct: number | null;
  handoff_rate_d1: number;
  handoff_rate_vs_d2_pp: number | null;
  chats_landed_d1: number;
  largest_bucket: {
    id: string;
    label: string;
    count: number;
    pct: number;
  } | null;
};

export type HandoffAnalysis = {
  reference_date: string;
  focus_date: string;
  kpis: HandoffKpis;
  movers: { rising: HandoffMover[]; falling: HandoffMover[] };
  day_headers: HandoffDayHeader[];
  buckets: HandoffMatrixBucket[];
  volume?: Record<string, number>;
  day_labels?: string[];
  bucket_order?: string[];
};

export async function getHandoffAnalysis(): Promise<HandoffAnalysis> {
  return apiJson<HandoffAnalysis>("/api/responder-evals/hand-off-analysis");
}

export type HandoffChatIds = {
  since: string;
  until: string;
  total: number;
  chat_ids: string[];
};

export async function getHandoffChatIds(
  bucket: string,
  subBucket?: string,
  opts?: { scope?: "day" | "last_7" | "mtd"; day?: string }
): Promise<HandoffChatIds> {
  const q = new URLSearchParams({ bucket });
  if (subBucket) q.set("sub_bucket", subBucket);
  if (opts?.scope) q.set("scope", opts.scope);
  if (opts?.day) q.set("day", opts.day);
  return apiJson<HandoffChatIds>(`/api/responder-evals/hand-off-analysis/chat-ids?${q}`);
}

export type JitHoldOrderStatus = "triggered" | "split_done" | "kept_original";

export type JitHoldOrderRow = {
  parent_order_id: string;
  status: JitHoldOrderStatus;
  triggered_at: string;
  updated_at: string;
};

export type JitHoldOrderList = {
  items: JitHoldOrderRow[];
  total: number;
  stats: {
    total: number;
    triggered: number;
    split_done: number;
    kept_original: number;
  };
};

export async function listJitHoldOrders(params?: {
  days?: number;
  limit?: number;
  offset?: number;
  status?: JitHoldOrderStatus | "";
  search?: string;
}): Promise<JitHoldOrderList> {
  const q = new URLSearchParams();
  q.set("days", String(params?.days ?? 7));
  q.set("limit", String(params?.limit ?? 50));
  q.set("offset", String(params?.offset ?? 0));
  if (params?.status) q.set("status", params.status);
  if (params?.search) q.set("search", params.search);
  return apiJson<JitHoldOrderList>(`/api/responder-evals/jit-hold-orders?${q}`);
}

/** Stored TEXT[] tag for a chart attribute id (inverse of dashboard _attr_id). */
export function responderEvalReasonTagValue(parentIssue: string, attrId: string): string {
  if (attrId.startsWith("severity_")) {
    return `hallucination_severity:${attrId.slice("severity_".length)}`;
  }
  if (parentIssue === "traceability_failure") {
    return `traceability:${attrId}`;
  }
  if (parentIssue === "hard_gate:pii") {
    return attrId.startsWith("pii:") ? attrId : `pii:${attrId}`;
  }
  if (parentIssue === "hallucination" || parentIssue.startsWith("hard_gate:hallucination")) {
    return `hallucination:${attrId}`;
  }
  if (parentIssue.startsWith("resolution:")) {
    return `resolution_lifecycle:${attrId}`;
  }
  return attrId;
}

export type ResponderEvalReasonGroup = {
  parentValue: string;
  parentLabel: string;
  parentCount: number;
  options: { value: string; label: string; count: number }[];
};

/** Top-N issue options for the eval-runs Reason filter (same source as chart panels). */
export function buildResponderEvalReasonGroups(
  topIssues: ResponderEvalTopIssues | null,
  segment: ResponderEvalSegment
): ResponderEvalReasonGroup[] {
  const block = topIssues?.[segment];
  if (!block) return [];
  const merged = new Map<string, { count: number; attrs: Map<string, number> }>();
  for (const g of RESPONDER_EVAL_ISSUE_GRADES) {
    for (const item of block[g] ?? []) {
      const row = merged.get(item.issue) ?? { count: 0, attrs: new Map<string, number>() };
      row.count += item.count;
      for (const a of item.attributes ?? []) {
        row.attrs.set(a.id, (row.attrs.get(a.id) ?? 0) + a.count);
      }
      merged.set(item.issue, row);
    }
  }
  return Array.from(merged.entries())
    .sort((a, b) => b[1].count - a[1].count)
    .map(([issue, data]) => ({
      parentValue: issue,
      parentLabel: evalIssueLabel(issue),
      parentCount: data.count,
      options: Array.from(data.attrs.entries())
        .sort((a, b) => b[1] - a[1])
        .map(([id, count]) => ({
          value: responderEvalReasonTagValue(issue, id),
          label: evalIssueAttributeLabel(issue, id),
          count,
        })),
    }));
}
