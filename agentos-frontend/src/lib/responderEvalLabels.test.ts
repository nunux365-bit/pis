import { describe, expect, it } from "vitest";
import { evalIssueAttributeLabel, evalIssueLabel } from "./responderEvalLabels";

describe("evalIssueLabel", () => {
  it("maps guardrail and policy issue keys", () => {
    expect(evalIssueLabel("guardrail:no_solution_before_concern")).toBe("No solution before concern");
    expect(evalIssueLabel("policy:tone")).toBe("Policy: Tone / professionalism");
    expect(evalIssueLabel("policy:unsupported_status_or_eta")).toBe(
      "Policy: Unsupported status/ETA"
    );
  });

  it("maps hard gates and resolution", () => {
    expect(evalIssueLabel("hard_gate:hallucination")).toBe("Hallucination hard gate");
    expect(evalIssueLabel("hard_gate:hallucination_P0")).toBe("Critical hallucination (P0)");
    expect(evalIssueLabel("hard_gate:pii")).toBe("PII leak (after redaction)");
    expect(evalIssueLabel("hard_gate:inherited_pii")).toBe(
      "Grade E forced by chat PII (not attributed to this speaker)"
    );
    expect(evalIssueLabel("hard_gate:inherited_hallucination")).toBe(
      "Grade E forced by chat hallucination (not this speaker's turns)"
    );
    expect(evalIssueLabel("resolution:no")).toBe("Resolution: no");
    expect(evalIssueLabel("traceability_failure")).toBe("Traceability failure");
  });

  it("maps bare hallucination flag", () => {
    expect(evalIssueLabel("hallucination")).toBe("Hallucination");
  });

  it("maps nested attribute labels", () => {
    expect(evalIssueAttributeLabel("hard_gate:hallucination", "false_refund")).toBe(
      "False refund claim"
    );
    expect(evalIssueAttributeLabel("hallucination", "false_refund")).toBe("False refund claim");
    expect(evalIssueAttributeLabel("hallucination", "severity_P0")).toBe("Severity P0");
    expect(evalIssueAttributeLabel("hallucination", "severity_soft")).toBe("Soft (no hard gate)");
    expect(evalIssueAttributeLabel("hard_gate:pii", "email")).toBe("Email");
    expect(evalIssueAttributeLabel("hard_gate:pii", "field:fe_phone_number")).toBe(
      "Field: fe phone number"
    );
    expect(evalIssueAttributeLabel("traceability_failure", "nuance_or_paraphrase")).toBe(
      "Nuance / paraphrase mismatch"
    );
    expect(evalIssueAttributeLabel("resolution:partial", "in_transit")).toBe("Lifecycle · In transit");
    expect(evalIssueLabel("guardrail_violations")).toBe(
      "Guardrail count (rule turns not attributed to this speaker)"
    );
  });
});
