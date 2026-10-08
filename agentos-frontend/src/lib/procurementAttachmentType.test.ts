import { describe, expect, it } from "vitest";
import { procurementAttachmentTypeAllowed } from "./procurementApi";

describe("procurementAttachmentTypeAllowed", () => {
  it("allows canonical types", () => {
    expect(procurementAttachmentTypeAllowed("a.pdf", "application/pdf")).toBe(true);
    expect(procurementAttachmentTypeAllowed("a.jpg", "image/jpeg")).toBe(true);
    expect(procurementAttachmentTypeAllowed("a.png", "image/png")).toBe(true);
    expect(procurementAttachmentTypeAllowed("a.xls", "application/vnd.ms-excel")).toBe(true);
    expect(
      procurementAttachmentTypeAllowed(
        "a.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
      )
    ).toBe(true);
  });

  it("strips MIME parameters and accepts aliases", () => {
    expect(procurementAttachmentTypeAllowed("a.pdf", "application/pdf; charset=binary")).toBe(true);
    expect(procurementAttachmentTypeAllowed("a.jpg", "image/pjpeg")).toBe(true);
    expect(procurementAttachmentTypeAllowed("a.xls", "application/excel")).toBe(true);
    expect(procurementAttachmentTypeAllowed("a.xlsx", "application/x-excel")).toBe(true);
  });

  it("falls back to extension for empty/octet-stream", () => {
    expect(procurementAttachmentTypeAllowed("shot.png", "")).toBe(true);
    expect(procurementAttachmentTypeAllowed("sheet.xlsx", "application/octet-stream")).toBe(true);
  });

  it("rejects dangerous double extension and disallowed types", () => {
    expect(procurementAttachmentTypeAllowed("evil.exe.pdf", "application/octet-stream")).toBe(false);
    expect(procurementAttachmentTypeAllowed("a.gif", "image/gif")).toBe(false);
    expect(procurementAttachmentTypeAllowed("a.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")).toBe(
      false
    );
  });
});
