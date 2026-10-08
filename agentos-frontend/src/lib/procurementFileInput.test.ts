import { describe, expect, it } from "vitest";
import {
  appendProcurementFilePicks,
  mergeProcurementFileInputSelection,
  PROCUREMENT_MAX_FILES,
  readProcurementFileInput,
  removeProcurementFileAtIndex,
} from "./procurementApi";

function mockInput(files: File[]): HTMLInputElement {
  const input = { files: null as FileList | null, value: "C:\\fakepath\\x.pdf" } as HTMLInputElement;
  const list = {
    length: files.length,
    item: (i: number) => files[i] ?? null,
    [Symbol.iterator]: function* () {
      for (const f of files) yield f;
    },
  } as FileList;
  for (let i = 0; i < files.length; i++) {
    Object.defineProperty(list, String(i), { value: files[i], enumerable: true });
  }
  input.files = list;
  return input;
}

describe("readProcurementFileInput", () => {
  it("returns picked files and clears the input", () => {
    const file = new File(["a"], "a.pdf", { type: "application/pdf" });
    const input = mockInput([file]);

    expect(readProcurementFileInput(input)).toEqual([file]);
    expect(input.value).toBe("");
  });
});

describe("appendProcurementFilePicks", () => {
  it("appends valid picks to prior selection", () => {
    const a = new File(["a"], "a.pdf", { type: "application/pdf" });
    const b = new File(["b"], "b.pdf", { type: "application/pdf" });

    const result = appendProcurementFilePicks([a], [b]);

    expect(result.files).toEqual([a, b]);
    expect(result.errors).toEqual([]);
  });

  it("survives repeated pure merges (Strict Mode style)", () => {
    const a = new File(["a"], "a.pdf", { type: "application/pdf" });
    const picked = [a];
    const prev: File[] = [];

    const first = appendProcurementFilePicks(prev, picked);
    const second = appendProcurementFilePicks(prev, picked);

    expect(first.files).toEqual([a]);
    expect(second.files).toEqual([a]);
  });

  it("rejects unsupported file types with a message", () => {
    const bad = new File(["x"], "notes.doc", { type: "application/msword" });

    const result = appendProcurementFilePicks([], [bad]);

    expect(result.files).toEqual([]);
    expect(result.errors[0]).toMatch(/PDF, JPG, PNG, XLS, or XLSX/i);
  });

  it("enforces the max file count", () => {
    const existing = Array.from(
      { length: PROCUREMENT_MAX_FILES },
      (_, i) => new File(["x"], `f${i}.pdf`, { type: "application/pdf" })
    );
    const extra = new File(["y"], "extra.pdf", { type: "application/pdf" });

    const result = appendProcurementFilePicks(existing, [extra]);

    expect(result.files).toHaveLength(PROCUREMENT_MAX_FILES);
    expect(result.errors[0]).toMatch(/at most 10 files/i);
  });
});

describe("mergeProcurementFileInputSelection", () => {
  it("appends new picks to prior selection", () => {
    const a = new File(["a"], "a.pdf", { type: "application/pdf" });
    const b = new File(["b"], "b.pdf", { type: "application/pdf" });
    const input = mockInput([b]);

    const next = mergeProcurementFileInputSelection([a], input);

    expect(next).toEqual([a, b]);
    expect(input.value).toBe("");
  });
});

describe("removeProcurementFileAtIndex", () => {
  it("removes the file at the given index", () => {
    const a = new File(["a"], "a.pdf", { type: "application/pdf" });
    const b = new File(["b"], "b.pdf", { type: "application/pdf" });
    expect(removeProcurementFileAtIndex([a, b], 0)).toEqual([b]);
    expect(removeProcurementFileAtIndex([a, b], 1)).toEqual([a]);
  });

  it("matches the staged upload payload after removing one pick", () => {
    const keep = new File(["keep"], "keep.pdf", { type: "application/pdf" });
    const drop = new File(["drop"], "drop.pdf", { type: "application/pdf" });
    const staged = [keep, drop];

    const uploadPayload = removeProcurementFileAtIndex(staged, 1);

    expect(uploadPayload).toEqual([keep]);
    expect(uploadPayload.some((f) => f.name === "drop.pdf")).toBe(false);
  });
});
