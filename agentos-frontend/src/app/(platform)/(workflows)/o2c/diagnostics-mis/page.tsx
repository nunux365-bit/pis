import { WorkflowPlaceholder } from "@/components/approvals/WorkflowPlaceholder";

export default function O2CDiagnosticsMisPage() {
  return (
    <WorkflowPlaceholder title="Diagnostics MIS">
      <p>
        This review workflow is not wired up yet. Use{" "}
        <span className="font-semibold text-[var(--text-primary)]">OHC MIS</span> under Order to cash
        for live reviews.
      </p>
    </WorkflowPlaceholder>
  );
}
